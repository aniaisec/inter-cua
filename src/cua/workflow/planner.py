"""Which version of each capability a workflow runs, and each step's inputs.

Planning is deterministic and model-free: every step's capability is
resolved in the registry the way a call by name is (``cua.registry.resolver``:
the pinned version, else the highest approved one), and the resolved
versions are the plan. A retried request runs the plan its first attempt
ran (``pins``), so a version approved in between does not change what a
retry does halfway through.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cua.artifact.schema import Capability
from cua.registry.models import Status
from cua.registry.resolver import Unresolvable, resolve
from cua.registry.store import Registry
from cua.replay.result import OutputValue
from cua.workflow.models import StepSpec, Workflow, WorkflowError, parse_ref


@dataclass(frozen=True)
class PlannedStep:
    spec: StepSpec
    path: Path
    capability: Capability
    status: Status

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def version(self) -> int:
        return self.capability.version

    @property
    def needs_consent(self) -> bool:
        """A step of it commits: an approval token, or a person, must say yes."""
        return any(s.approval == "required" or s.risk != "safe" for s in self.capability.steps)

    @property
    def idempotent(self) -> bool:
        return self.capability.contract.idempotent and not self.needs_consent


@dataclass(frozen=True)
class Plan:
    workflow: Workflow
    steps: list[PlannedStep]

    def step(self, step_id: str) -> PlannedStep:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    @property
    def pins(self) -> dict[str, int]:
        return {s.id: s.version for s in self.steps}

    @property
    def idempotent(self) -> bool:
        return all(s.idempotent for s in self.steps)


def plan(workflow: Workflow, registry: Registry, pins: dict[str, int] | None = None) -> Plan:
    steps = []
    for spec in workflow.steps:
        version = pins.get(spec.id, spec.version) if pins else spec.version
        try:
            v = resolve(registry, spec.capability, version)
        except Unresolvable as exc:
            raise WorkflowError(f"step {spec.id!r}: {exc}") from None
        steps.append(PlannedStep(spec, v.path, v.capability, v.status))
    return Plan(workflow, steps)


def step_inputs(
    step: PlannedStep,
    inputs: dict[str, str],
    outputs: dict[str, dict[str, OutputValue]],
) -> dict[str, str]:
    """The step's capability inputs, from the workflow's inputs and the
    outputs of the steps before it. A binding whose value is absent (an
    optional workflow input not given, an optional output not read) is left
    out, and the capability's own input check decides whether that is
    allowed; the validator has already refused binding one to a required
    input."""
    out: dict[str, str] = {}
    for name, text in step.spec.inputs.items():
        ref = parse_ref(text)
        if ref.kind == "literal":
            assert ref.value is not None
            out[name] = ref.value
        elif ref.kind == "input":
            if ref.name in inputs:
                out[name] = inputs[ref.name]
        else:
            assert ref.step is not None
            value = outputs.get(ref.step, {}).get(ref.name)
            if value is not None:
                out[name] = str(value)
    return out


def static_inputs(step: PlannedStep, inputs: dict[str, str]) -> dict[str, str] | None:
    """The step's inputs if they are known before anything runs (no binding
    reads another step's output), else None."""
    if any(parse_ref(t).kind == "output" for t in step.spec.inputs.values()):
        return None
    return step_inputs(step, inputs, {})
