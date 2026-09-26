"""Everything checked about a workflow before any step runs.

``problems``: the definition is wrong, whoever runs it — a reference to an
input or output that does not exist, or to a step that has not run yet; a
type that does not fit; an optional value bound to a required input; a
sensitive value bound where it would be logged in the clear. Found at plan
time, so a mistake in the wiring never shows up halfway through, after a
commit.

``refusals``: the definition is fine but this deployment will not run it
now — a step's version is a draft or revoked, is for another app family, or
drives a surface this build has no adapter for. Each is exactly what that
step's own replay would refuse with ``POLICY_BLOCKED``; checking them for
every step first means an earlier step is not run for nothing.

Types: a value flows only into an input of the same type, or an ``integer``
into a ``decimal``. Output values are typed by the capability's own output
declarations (``decimal`` travels as an exact string, ``integer`` as an int).
"""

from __future__ import annotations

from collections.abc import Iterable

from cua.artifact.schema import InputSpec, ValueType
from cua.registry import lifecycle
from cua.registry.resolver import refusal as lifecycle_refusal
from cua.replay.invocation import check_input
from cua.surface import SUPPORTED_SURFACES
from cua.tenant import Tenant
from cua.workflow.models import Ref, parse_ref
from cua.workflow.planner import Plan


def assignable(source: ValueType, target: ValueType) -> bool:
    return source == target or (source, target) == ("integer", "decimal")


def problems(plan: Plan) -> list[str]:
    wf = plan.workflow
    out: list[str] = []
    ids = [s.id for s in wf.steps]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        out.append(f"step id {dup!r} is used more than once")
    used: set[str] = set()
    earlier: dict[str, int] = {}
    for index, step in enumerate(plan.steps):
        cap = step.capability
        where = f"step {step.id!r} ({cap.name} v{cap.version})"
        for name, text in step.spec.inputs.items():
            target = cap.inputs.get(name)
            if target is None:
                declared = ", ".join(sorted(cap.inputs)) or "none"
                out.append(
                    f"{where}: {name!r} is not an input of {cap.name} (declared: {declared})"
                )
                continue
            try:
                ref = parse_ref(text)
            except ValueError as exc:
                out.append(f"{where}: input {name!r}: {exc}")
                continue
            if ref.kind == "input":
                used.add(ref.name)
            out += _binding(plan, where, name, target, ref, index, earlier)
        for name, spec in cap.inputs.items():
            if spec.required and name not in step.spec.inputs:
                out.append(f"{where}: required input {name!r} is not bound")
        earlier.setdefault(step.id, index)

    for name, text in wf.outputs.items():
        try:
            ref = parse_ref(text)
        except ValueError as exc:
            out.append(f"output {name!r}: {exc}")
            continue
        if ref.kind == "literal":
            out.append(f"output {name!r}: a workflow output is read from a step or an input")
        elif ref.kind == "input":
            used.add(ref.name)
            if ref.name not in wf.inputs:
                out.append(f"output {name!r}: {ref} is not a workflow input")
            elif wf.inputs[ref.name].sensitive:
                out.append(
                    f"output {name!r}: {ref} is sensitive and would be returned in the clear"
                )
        else:
            out += _output_ref(plan, f"output {name!r}", ref, len(plan.steps), earlier)

    for name in sorted(set(wf.inputs) - used):
        out.append(f"workflow input {name!r} is declared and never used")
    return out


def _binding(
    plan: Plan,
    where: str,
    name: str,
    target: InputSpec,
    ref: Ref,
    index: int,
    earlier: dict[str, int],
) -> list[str]:
    wf = plan.workflow
    at = f"{where}: input {name!r}"
    if ref.kind == "literal":
        assert ref.value is not None
        problem = check_input(name, ref.value, target)
        return [f"{where}: {problem}"] if problem else []
    if ref.kind == "input":
        source = wf.inputs.get(ref.name)
        if source is None:
            return [f"{at}: {ref} is not a workflow input (declared: {_names(wf.inputs)})"]
        out = []
        if not assignable(source.type, target.type):
            out.append(f"{at} is {target.type}; {ref} is {source.type}")
        if target.required and not source.required:
            out.append(f"{at} is required; {ref} is optional")
        if target.sensitive and not source.sensitive:
            out.append(f"{at} is sensitive; declare {ref} sensitive too, so it is masked here")
        if source.sensitive and not target.sensitive:
            out.append(f"{at} is not sensitive: the step would log {ref} in the clear")
        return out
    found = _output_ref(plan, at, ref, index, earlier)
    if found:
        return found
    assert ref.step is not None
    spec = plan.step(ref.step).capability.outputs[ref.name]
    out = []
    if not assignable(spec.type, target.type):
        out.append(f"{at} is {target.type}; {ref} is {spec.type}")
    if target.required and spec.optional:
        out.append(f"{at} is required; {ref} is optional and may not be read")
    return out


def _output_ref(plan: Plan, at: str, ref: Ref, index: int, earlier: dict[str, int]) -> list[str]:
    assert ref.step is not None
    ids = [s.id for s in plan.steps]
    if ref.step not in ids:
        return [f"{at}: {ref} names no step (steps: {', '.join(ids)})"]
    if ref.step not in earlier or earlier[ref.step] >= index:
        return [f"{at}: {ref} is read before step {ref.step!r} has run"]
    cap = plan.step(ref.step).capability
    if ref.name not in cap.outputs:
        return [f"{at}: {cap.name} has no output {ref.name!r} (outputs: {_names(cap.outputs)})"]
    return []


def output_types(plan: Plan) -> dict[str, tuple[ValueType, bool]]:
    """Each workflow output's type, and whether it may be absent."""
    out: dict[str, tuple[ValueType, bool]] = {}
    for name, text in plan.workflow.outputs.items():
        ref = parse_ref(text)
        if ref.kind == "input":
            spec = plan.workflow.inputs[ref.name]
            out[name] = (spec.type, not spec.required)
        elif ref.kind == "output" and ref.step is not None:
            o = plan.step(ref.step).capability.outputs[ref.name]
            out[name] = (o.type, o.optional)
    return out


def refusals(plan: Plan, tenant: Tenant) -> list[str]:
    out = []
    for step in plan.steps:
        cap = step.capability
        where = f"step {step.id!r}: {cap.name} v{cap.version}"
        if cap.approval_state != "approved" or not lifecycle.may_start(step.status):
            why = lifecycle_refusal(cap.name, cap.version, step.status)
            out.append(
                f"{where} is {step.status}; a workflow runs only approved capabilities"
                + (f" ({why})" if why else "")
            )
        if cap.target.app_family != tenant.app_family:
            out.append(
                f"{where} is for app family {cap.target.app_family!r}; tenant {tenant.id!r} "
                f"runs {tenant.app_family!r}"
            )
        if cap.target.surface not in SUPPORTED_SURFACES:
            out.append(f"{where} drives a {cap.target.surface!r} surface this build cannot drive")
    return out


def _names(names: Iterable[str]) -> str:
    return ", ".join(sorted(names)) or "none"
