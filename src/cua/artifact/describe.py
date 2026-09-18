"""A capability in plain words, for the person who has to approve it.

The artifact is JSON because an engine replays it; the approver is usually not
an engineer. This renders what they need to say yes or no to: what it is for,
what it needs and returns, whether it changes anything, every step with how
its control is found, and every way it can end. Nothing here is hidden
behind a field name, and nothing is left out — an approval covers exactly
what was shown.

Plain ASCII on purpose: it is read in terminals and redirected to files on
machines whose console encoding is not UTF-8.
"""

from __future__ import annotations

import re

from cua.artifact.schema import (
    DONE,
    PLACEHOLDER,
    Capability,
    Detector,
    InputSpec,
    OutputSpec,
    StepTimedOut,
)
from cua.artifact.schema import Step as ArtifactStep
from cua.surface.conditions import (
    AllOf,
    AnyOf,
    Condition,
    ErrorBannerPresent,
    LocationMatches,
    OutputExtracted,
    RegionPresent,
    TextPresent,
    ValidationMessagePresent,
    ValueSet,
    Visible,
)
from cua.surface.locators import BBox, NearText, RoleName, TableCell, Within

_CREDENTIAL = re.compile(r"^credentials\.([a-z0-9_]+)\.([a-z0-9_]+)$")
_TYPES = {"string": "text", "decimal": "decimal number", "integer": "whole number"}
_PARSE = {
    "text": "as text",
    "decimal": "as a number",
    "currency_usd": "as US dollars",
    "integer": "as a whole number",
}
_SIDE_EFFECTS = {
    "none": "none - it only reads",
    "creates_record": "CREATES A RECORD in the system of record",
    "modifies_record": "CHANGES A RECORD in the system of record",
    "moves_money": "MOVES MONEY",
}
_THEN = {
    "continue": "then carry on",
    "retry_step": "then try the step again",
    "restart_from_last_checkpoint": "then restart from the last checkpoint passed",
}
_INDENT = " " * 22


def describe(cap: Capability) -> str:
    out: list[str] = []
    _header(cap, out)
    _inputs(cap, out)
    _outputs(cap, out)
    _steps(cap, out)
    _outcomes(cap, out)
    _provenance(cap, out)
    return "\n".join(out) + "\n"


# -- sections -------------------------------------------------------------------


def _header(cap: Capability, out: list[str]) -> None:
    if cap.approval_state == "approved":
        status = f"APPROVED by {cap.approved_by}" + (
            f" at {cap.approved_at}" if cap.approved_at else ""
        )
    else:
        status = "DRAFT - not approved; it will not run unattended"
    target, contract = cap.target, cap.contract
    risky = [s.id for s in cap.steps if s.approval == "required"]
    out += [
        f"Capability    {cap.name}  (version {cap.version})",
        f"Status        {status}",
        f"Purpose       {cap.description}",
        f"Application   {target.vendor} {target.version_hint} (app family {target.app_family}), "
        f"{target.surface}; starts at {target.entry.pattern}",
        f"Side effects  {_SIDE_EFFECTS[contract.side_effects]}. "
        + (
            "Safe to run again with the same inputs."
            if contract.idempotent
            else "NOT safe to simply run again: a second run may repeat the change."
        ),
        "Human needed  "
        + (
            f"yes - {', '.join(risky)} must be approved (an approval token, else a person)."
            if risky
            else "only if something goes wrong."
        ),
        "Session       "
        + (
            "starts from a fresh sign-on."
            if contract.preconditions.session == "fresh"
            else "may reuse a signed-on session."
        ),
    ]


def _inputs(cap: Capability, out: list[str]) -> None:
    out += ["", "Inputs (the caller supplies these)"]
    if not cap.inputs:
        out.append("  none")
    for name, spec in cap.inputs.items():
        out.append(f"  {name:<20}{_input(spec)}")
    if cap.credentials:
        out += [
            "",
            "Sign-in (resolved by the runner from the tenant's secret store; "
            "never shown, stored or logged)",
        ]
        for name, cred in cap.credentials.items():
            out.append(f"  {name:<20}{', '.join(cred.fields)} from {cred.ref}")


def _input(spec: InputSpec) -> str:
    bits = [_TYPES[spec.type], "required" if spec.required else "optional"]
    if spec.pattern:
        bits.append(f"must match {spec.pattern}")
    if spec.sensitive:
        bits.append("sensitive: masked in logs and screenshots")
    text = ", ".join(bits)
    return f"{text} - {spec.description}" if spec.description else text


def _outputs(cap: Capability, out: list[str]) -> None:
    out += ["", "Outputs (returned on success)"]
    if not cap.outputs:
        out.append("  none")
    for name, spec in cap.outputs.items():
        out.append(f"  {name:<20}{_output(spec)}")
        rungs = spec.extract.target
        out.append(f"{_INDENT}read from {_rung(rungs[0])}, {_PARSE[spec.extract.parse]}")
        for rung in rungs[1:]:
            out.append(f"{_INDENT}  else {_rung(rung)}")


def _output(spec: OutputSpec) -> str:
    text = f"{_TYPES[spec.type]}, {'optional' if spec.optional else 'required'}"
    return f"{text} - {spec.description}" if spec.description else text


def _steps(cap: Capability, out: list[str]) -> None:
    out += ["", "Steps (each control is found by the first description that matches it exactly)"]
    after: dict[str, list[str]] = {}
    for cp in cap.checkpoints:
        after.setdefault(cp.after_step, []).append(
            f"CHECKPOINT {cp.id}: " + "; ".join(_condition(c) for c in cp.all_of)
        )
    for n, step in enumerate(cap.steps, start=1):
        head = f"  {n}. {step.id}"
        out.append(f"{head:<22}{_action(step)}")
        if step.intent:
            out.append(f"{_INDENT}why: {step.intent}")
        if step.target:
            out.append(f"{_INDENT}found by: {_rung(step.target[0])}  [{step.target[0].strategy}]")
            for rung in step.target[1:]:
                out.append(f"{_INDENT}  else {_rung(rung)}  [{rung.strategy}]")
        if step.expect_after is not None:
            out.append(f"{_INDENT}then expects: {_condition(step.expect_after)}")
        out += [f"{_INDENT}{line}" for line in _safety(step)]
        out += [f"{_INDENT}{line}" for line in after.get(step.id, [])]
    for line in after.get(DONE, []):
        out.append(f"  end{'':<17}{line}")


def _action(step: ArtifactStep) -> str:
    target = _rung(step.target[0]) if step.target else "the page"
    if step.action == "type":
        return f"Type {_value(step.value or '')} into {target}."
    if step.action == "select":
        return f"Choose {_value(step.value or '')} in {target}."
    if step.action == "press":
        return f"Press {step.key}" + (f" in {target}." if step.target else ".")
    if step.action == "read":
        return f"Read {target}."
    return f"Click {target}."


def _safety(step: ArtifactStep) -> list[str]:
    lines: list[str] = []
    if step.risk == "irreversible":
        lines.append("!! COMMITS A CHANGE. Needs approval; never retried automatically.")
    elif step.risk == "risky":
        lines.append("!! Risky step.")
    elif step.approval == "required":
        lines.append("!! Needs approval.")
    if step.side_effect_marker is not None:
        lines.append(f"if it fails, it counts as done when: {_condition(step.side_effect_marker)}")
    if step.on_fail == "escalate":
        lines.append("if it cannot be done: hand the live session to a person")
    else:
        lines.append("if it cannot be done: stop and report a failure")
    return lines


def _outcomes(cap: Capability, out: list[str]) -> None:
    out += ["", "How it can end"]
    names = ", ".join(cap.outputs) or "nothing"
    out.append(f"  {'success':<20}returns {names}")
    by_class: dict[str, list[Detector]] = {}
    for det in cap.outcome_detectors:
        by_class.setdefault(det.class_, []).append(det)
    for det in by_class.get("business", []):
        payload = cap.contract.outcomes.get(det.code)
        extra = f"; reports {', '.join(payload.payload)}" if payload and payload.payload else ""
        out.append(f"  {det.code:<20}business outcome - {_when(det)}{extra}")
    for det in by_class.get("hard", []):
        out.append(f"  {det.code:<20}failure - {_when(det)}")
    out.append(
        f"  {'(any step)':<20}failure - a control cannot be found, or found more than once, "
        "or a check does not hold"
    )
    recoverable = by_class.get("recoverable", [])
    if recoverable:
        out += ["", "Handled automatically (the run carries on)"]
        for det in recoverable:
            assert det.recover is not None
            action = ""
            if det.recover.run:
                rec = cap.recoverers.get(det.recover.run)
                action = det.recover.run.replace("_", " ")
                if rec is not None and rec.sub_flow:
                    action += f" ({', '.join(rec.sub_flow)})"
                action += ", "
            limit = ""
            if det.recover.max > 1 or det.recover.backoff_s:
                limit = f" (up to {det.recover.max} times, {det.recover.backoff_s:g}s apart)"
            only = ", only on steps marked retryable" if det.recover.then == "retry_step" else ""
            then = _THEN[det.recover.then]
            if not action:
                then = then.removeprefix("then ")
            out.append(f"  {det.code:<20}{_when(det)}: {action}{then}{limit}{only}")
        limits = cap.recovery_limits
        out.append(
            f"  {'':<20}at most {limits.per_step} per step and {limits.per_run} per run; "
            "beyond that the run fails"
        )


def _when(det: Detector) -> str:
    if isinstance(det.match, StepTimedOut):
        what = "a step takes too long"
    else:
        what = _condition(det.match)
    scope = det.scope
    if scope is None or (scope.after_step is None and scope.after_checkpoint is None):
        return f"whenever {what}"
    if scope.after_step is not None:
        return f"after {scope.after_step}, if {what}"
    return f"once past {scope.after_checkpoint}, if {what}"


def _provenance(cap: Capability, out: list[str]) -> None:
    p = cap.provenance
    out += [
        "",
        "Where it came from",
        f"  discovered in run {p.discovery_run_id} by {p.model} ({p.provider}), "
        f"recorded {p.recorded_at}",
        f"  transcript sha256 {p.transcript_sha256} (the transcript stays in evidence, "
        "not in this file)",
    ]
    if cap.recording_env is not None:
        vp = cap.recording_env.viewport
        out.append(f"  screen positions were measured at {vp.w}x{vp.h}; they are used only there")


# -- phrases --------------------------------------------------------------------


def _pane(within: Within | None) -> str:
    return f" in the {within.frame} pane" if within and within.frame else ""


def _rung(rung: RoleName | NearText | TableCell | BBox) -> str:
    if isinstance(rung, RoleName):
        return f'the {rung.role} named "{rung.name}"{_pane(rung.within)}'
    if isinstance(rung, NearText):
        where = {
            None: "next to",
            "right": "to the right of",
            "below": "below",
            "left": "to the left of",
            "above": "above",
        }[rung.direction]
        return f'the {rung.role or "control"} {where} "{rung.text}"{_pane(rung.within)}'
    if isinstance(rung, TableCell):
        return (
            f'the "{rung.column_header}" column of the row with "{rung.row_contains}"'
            f"{_pane(rung.within)}"
        )
    return (
        f"the {rung.role or 'element'} at screen position ({rung.x:.0f}, {rung.y:.0f})"
        f"{_pane(rung.within)} - last resort, recorded screen size only"
    )


def _value(value: str) -> str:
    found = PLACEHOLDER.fullmatch(value)
    if found is None:
        if PLACEHOLDER.search(value):
            return f'"{value}" (with the placeholders filled in)'
        return f'the fixed text "{value}"'
    name = found.group(1)
    cred = _CREDENTIAL.match(name)
    if cred:
        return f"the {cred.group(1)} {cred.group(2)} (secret)"
    return f"the caller's {name}"


def _placeholders(text: str) -> str:
    return PLACEHOLDER.sub(lambda m: f"<the caller's {m.group(1)}>", text)


def _location(pattern: str) -> str:
    shown = pattern.removesuffix("$").replace("[0-9]+", "<number>").replace("[^/]+", "<value>")
    return shown.replace("\\", "")


def _condition(cond: Condition) -> str:
    if isinstance(cond, AllOf):
        return " and ".join(_condition(c) for c in cond.all_of)
    if isinstance(cond, AnyOf):
        return "either " + " or ".join(_condition(c) for c in cond.any_of)
    if isinstance(cond, LocationMatches):
        return f"the screen is at {_location(cond.pattern)}{_pane(cond.within)}"
    if isinstance(cond, TextPresent):
        found = PLACEHOLDER.fullmatch(cond.text)
        if found:
            return f"the caller's {found.group(1)} is shown{_pane(cond.within)}"
        return f'"{_placeholders(cond.text)}" is shown{_pane(cond.within)}'
    if isinstance(cond, RegionPresent):
        return f'a "{cond.name}" heading is shown{_pane(cond.within)}'
    if isinstance(cond, ErrorBannerPresent):
        wording = f' or "{cond.text}" is shown' if cond.text else ""
        return f"the application reports an error (a server error{wording})"
    if isinstance(cond, ValidationMessagePresent):
        return "the application rejects what was entered"
    if isinstance(cond, ValueSet):
        if cond.value is not None:
            return f"the field holds {_value(cond.value)}"
        return "the field holds what was typed"
    if isinstance(cond, Visible):
        return "the control is visible"
    if isinstance(cond, OutputExtracted):
        return f"{cond.name} has been read"
    return "a pop-up dialog appeared" + (f' saying "{cond.text}"' if cond.text else "")
