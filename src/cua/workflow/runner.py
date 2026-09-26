"""Run a workflow: plan, check everything, then each step through replay.

In order, each check before anything more expensive, and all of them before
the first step runs:

1. load the workflow and plan it (``cua.workflow.planner``); a retry with the
   same idempotency key runs the versions its first attempt resolved;
2. **the wiring** — every reference, type, optional and sensitive value
   (``cua.workflow.validator.problems``). A wrong definition is a usage
   error (``WorkflowError``), like a malformed capability file;
3. **workflow inputs** — ``INPUT_INVALID``, nothing run;
4. **every step may run here** — approved, not revoked, this tenant's app
   family, a surface this build drives. ``POLICY_BLOCKED``, nothing run;
5. **idempotency** — a workflow with a committing step needs an idempotency
   key. The same key and request returns the stored result; the same key
   for another request is ``INPUT_INVALID``;
6. **consent** — a committing step needs its own approval token
   (``--approval <step>=<token>``), or a handoff channel for a person to
   give it. A token whose step's inputs are known before the run is verified
   now (signature, capability, content, tenant, these inputs, unspent), so
   a refused one refuses the workflow before its first step, not after it.

Then each step runs through ``cua.replay.runner.replay``, exactly as ``cua
replay`` runs it: the step's own approval gate, policy, consent, lifecycle,
budget and idempotency apply unchanged, because nothing here goes around
them. What the workflow adds is the wiring between steps and one rule: the
first step that does not succeed ends the workflow, and its kind is the
workflow's kind. A lookup that answers ``NOT_FOUND`` means the sub-account is
never opened.

**Idempotency, per step.** Each step runs under a key derived from the
workflow's (``cua.workflow.journal.step_key``), so a retried workflow is
answered step by step from replay's own cache, and a committed step is
never committed again. An escalated step is not started again: ``cua
resume`` finishes it, and running the workflow again with the same key
carries on from its answer.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from cua.artifact.store import CAPABILITIES_DIR
from cua.escalation.channel import HandoffSettings
from cua.evidence.logger import RUNS_DIR, append_jsonl, utc_now
from cua.policy import tokens
from cua.policy.allowlist import Policy
from cua.policy.tokens import SpentTokens, TokenRefused
from cua.registry.store import Registry
from cua.replay.engine import ReplayConfig
from cua.replay.invocation import ApprovalGrant, Budget, Invocation, check_input
from cua.replay.result import RESULT, ReplayResult, SideEffect
from cua.replay.runner import replay
from cua.surface.playwright_surface import PlaywrightSurface
from cua.tenant import Tenant
from cua.workflow import journal, validator
from cua.workflow.journal import Entry, Journal, StepEntry
from cua.workflow.models import (
    RefusalCode,
    StepResult,
    Workflow,
    WorkflowError,
    WorkflowResult,
    load_workflow,
    parse_ref,
)
from cua.workflow.planner import Plan, PlannedStep, plan, static_inputs, step_inputs

WORKFLOW_RUNS = "workflows"
SurfaceFactory = Callable[[], AbstractContextManager[PlaywrightSurface]]


class StepRunner(Protocol):
    """``cua.replay.runner.replay``'s signature, as far as a step uses it."""

    def __call__(
        self,
        path: Path,
        *,
        tenant: Tenant,
        policy: Policy,
        invocation: Invocation,
        runs_dir: Path = ...,
        config: ReplayConfig | None = ...,
        surface: SurfaceFactory | None = ...,
        environ: dict[str, str] | None = ...,
        handoff: HandoffSettings | None = ...,
    ) -> ReplayResult: ...


class WorkflowRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    inputs: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str | None = None
    approvals: dict[str, ApprovalGrant] = Field(default_factory=dict)
    """Step id -> consent for that step's commit, for this request only."""
    budget: Budget = Field(default_factory=Budget)
    """``timeout_s`` is for the whole workflow: each step gets what is left.
    ``max_recoveries`` and ``allow_escalation`` apply to every step."""
    inject: dict[str, str] = Field(default_factory=dict)
    """Demo only: step id -> a mock-app failure mode armed for that step."""


def run(
    path: Path,
    *,
    tenant: Tenant,
    policy: Policy,
    request: WorkflowRequest,
    capabilities_dir: Path = CAPABILITIES_DIR,
    runs_dir: Path = RUNS_DIR,
    config: ReplayConfig | None = None,
    surface: SurfaceFactory | None = None,
    handoff: HandoffSettings | None = None,
    environ: dict[str, str] | None = None,
    step_runner: StepRunner | None = None,
) -> WorkflowResult:
    """Raises ``WorkflowError`` for a workflow that cannot be planned or is
    wired wrong; returns a ``WorkflowResult`` for everything else."""
    workflow = load_workflow(path)
    key = request.idempotency_key
    book = Journal(runs_dir) if key else None
    fp = journal.fingerprint(
        workflow.name, workflow.version, workflow.content_hash(), request.inputs
    )
    entry = book.get(key) if book is not None and key is not None else None
    if entry is not None and entry.fingerprint != fp:
        return _refused(
            workflow,
            request,
            "INPUT_INVALID",
            f"idempotency key {key!r} was already used for a different request (another "
            "workflow, workflow content or inputs)",
        )
    if entry is not None and entry.result is not None:
        stored = WorkflowResult.model_validate(entry.result)
        return stored.model_copy(update={"cached": True})

    p = plan(workflow, Registry(capabilities_dir), entry.pins if entry else None)
    wrong = validator.problems(p)
    if wrong:
        raise WorkflowError(f"{path.as_posix()}: " + "; ".join(wrong))

    problems = _input_problems(workflow, request.inputs)
    if problems:
        return _refused(workflow, request, "INPUT_INVALID", "; ".join(problems))
    blocked = validator.refusals(p, tenant)
    if blocked:
        return _refused(workflow, request, "POLICY_BLOCKED", "; ".join(blocked))
    if key is None and not p.idempotent:
        committing = ", ".join(s.id for s in p.steps if not s.idempotent)
        return _refused(
            workflow,
            request,
            "INPUT_INVALID",
            f"step(s) {committing} commit a change: pass an idempotency key, so that a retry "
            "after a lost answer returns the first result instead of committing again",
        )
    # A committing step an earlier attempt reached is answered from that attempt
    # (its cache, its run directory) and never started again: no new consent.
    reached = set(entry.steps) if entry else set()
    refusal = _consent(p, request, tenant, runs_dir, environ, handoff, reached)
    if refusal is not None:
        code, message = refusal
        return _refused(workflow, request, code, message)

    return _execute(
        p,
        request=request,
        entry=entry
        or Entry(
            fingerprint=fp,
            workflow=workflow.name,
            workflow_version=workflow.version,
            pins=p.pins,
        ),
        book=book,
        tenant=tenant,
        policy=policy,
        runs_dir=runs_dir,
        config=config,
        surface=surface,
        handoff=handoff,
        environ=environ,
        step_runner=step_runner or replay,
    )


def _input_problems(workflow: Workflow, inputs: dict[str, str]) -> list[str]:
    problems = []
    for name in inputs:
        if name not in workflow.inputs:
            declared = ", ".join(sorted(workflow.inputs)) or "none"
            problems.append(f"{name!r} is not an input of {workflow.name} (declared: {declared})")
    for name, spec in workflow.inputs.items():
        if name not in inputs:
            if spec.required:
                problems.append(f"{name!r} is required")
            continue
        problem = check_input(name, inputs[name], spec)
        if problem:
            problems.append(problem)
    return problems


def _consent(
    p: Plan,
    request: WorkflowRequest,
    tenant: Tenant,
    runs_dir: Path,
    environ: dict[str, str] | None,
    handoff: HandoffSettings | None,
    reached: set[str],
) -> tuple[RefusalCode, str] | None:
    ids = {s.id for s in p.steps}
    for step_id in request.approvals:
        if step_id not in ids:
            return "INPUT_INVALID", f"an approval was given for {step_id!r}, which is no step"
        if not p.step(step_id).needs_consent:
            return "INPUT_INVALID", f"step {step_id!r} commits nothing; it takes no approval"
    for step in p.steps:
        if not step.needs_consent or step.id in reached:
            continue
        grant = request.approvals.get(step.id)
        if grant is None:
            if handoff is None:
                return (
                    "POLICY_BLOCKED",
                    f"step {step.id!r} ({step.capability.name}) commits a change and needs "
                    f"consent: pass --approval {step.id}=<token> (`cua workflow approval-token`), "
                    "or --handoff for a person to give it",
                )
            continue
        known = static_inputs(step, request.inputs)
        if known is None:
            continue  # its inputs come from an earlier step: its own replay checks the token
        try:
            approval = tokens.verify(
                grant.token,
                step.capability,
                tenant,
                known,
                key=tokens.signing_key(tenant, environ=environ),
            )
        except TokenRefused as exc:
            return "POLICY_BLOCKED", f"approval for step {step.id!r} refused: {exc}"
        if grant.approved_by is not None and grant.approved_by != approval.approved_by:
            return (
                "POLICY_BLOCKED",
                f"approval for step {step.id!r} refused: the token was signed for "
                f"{approval.approved_by!r}, not {grant.approved_by!r}",
            )
        used_by = SpentTokens(runs_dir).spent_by(approval)
        if used_by is not None:
            return (
                "POLICY_BLOCKED",
                f"approval for step {step.id!r} refused: this token was already used by "
                f"{used_by}; one consent covers one commit",
            )
    return None


def _execute(
    p: Plan,
    *,
    request: WorkflowRequest,
    entry: Entry,
    book: Journal | None,
    tenant: Tenant,
    policy: Policy,
    runs_dir: Path,
    config: ReplayConfig | None,
    surface: SurfaceFactory | None,
    handoff: HandoffSettings | None,
    environ: dict[str, str] | None,
    step_runner: StepRunner,
) -> WorkflowResult:
    wf = p.workflow
    key = request.idempotency_key
    run_id = f"wf_{ULID()}"
    run_dir = runs_dir / WORKFLOW_RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log = run_dir / "log.jsonl"
    seq = 0

    def event(name: str, /, **fields: Any) -> None:
        nonlocal seq
        seq += 1
        append_jsonl(log, {"seq": seq, "ts": utc_now(), "event": name, **fields})

    def save() -> None:
        if book is not None and key is not None:
            book.put(key, entry)

    _write_json(
        run_dir / "workflow.json",
        {
            "workflow_run_id": run_id,
            "kind": "workflow",
            "started_at": utc_now(),
            "workflow": {
                "name": wf.name,
                "version": wf.version,
                "content_sha256": wf.content_hash(),
            },
            "tenant": {"id": tenant.id, "app_family": tenant.app_family},
            "plan": [
                {
                    "id": s.id,
                    "capability": s.capability.name,
                    "version": s.version,
                    "content_sha256": s.capability.content_hash(),
                    "status": s.status,
                    "needs_consent": s.needs_consent,
                    "path": s.path.as_posix(),
                }
                for s in p.steps
            ],
            "request": {
                "inputs": {
                    n: ("***" if wf.inputs[n].sensitive else v) for n, v in request.inputs.items()
                },
                "idempotency_key": key,
                "approvals": {
                    sid: {"approved_by": g.approved_by, "token_sha256": g.token_sha256[:12]}
                    for sid, g in request.approvals.items()
                },
                "budget": request.budget.model_dump(),
                "inject": request.inject,
                "attempt": len(entry.attempts) + 1,
            },
            "handoff": handoff.model_dump() if handoff else None,
        },
    )
    entry.attempts.append(run_id)
    save()

    started = time.monotonic()
    outputs: dict[str, dict[str, Any]] = {}
    steps: list[StepResult] = []
    ending: ReplayResult | None = None
    ended_at: PlannedStep | None = None
    refusal: tuple[RefusalCode, str, SideEffect] | None = None

    for step in p.steps:
        step_key = journal.step_key(wf.name, key, step.id) if key else None
        prior = entry.steps.get(step.id)
        result = _earlier_answer(prior, step, runs_dir)
        if result is None and prior is not None and prior.state == "started":
            if not step.idempotent:
                refusal = (
                    "INTERRUPTED",
                    f"an earlier attempt started step {step.id!r} ({step.capability.name}) and "
                    "never reported; it may have committed. Find out what it did before "
                    "trying again (with a new idempotency key)",
                    "unknown",
                )
                ended_at = step
                break
        if result is None:
            remaining = request.budget.timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                refusal = (
                    "TIMEOUT",
                    f"the workflow's {request.budget.timeout_s:g} s were spent before step "
                    f"{step.id!r}",
                    "none",
                )
                ended_at = step
                break
            inputs = step_inputs(step, request.inputs, outputs)
            invocation = Invocation(
                inputs=inputs,
                idempotency_key=step_key,
                # A step an earlier attempt reached is answered from its cache: the
                # consent went into that attempt, and a token since expired must not
                # stand between the caller and a commit that happened.
                approval=request.approvals.get(step.id) if prior is None else None,
                budget=request.budget.model_copy(update={"timeout_s": remaining}),
                inject=request.inject.get(step.id),
            )
            entry.steps[step.id] = StepEntry(state="started", idempotency_key=step_key)
            save()
            event("step.start", step=step.id, capability=step.capability.name, version=step.version)
            try:
                result = step_runner(
                    step.path,
                    tenant=tenant,
                    policy=policy,
                    invocation=invocation,
                    runs_dir=runs_dir,
                    config=config,
                    surface=surface,
                    environ=environ,
                    handoff=handoff,
                )
            except Exception:
                # Raised before a browser could start (a missing credential):
                # nothing ran, so the step is not left looking interrupted.
                # An interruption inside the run is written by the run itself.
                if _find_step_run(runs_dir, step_key) is None:
                    entry.steps.pop(step.id, None)
                    save()
                raise

        entry.steps[step.id] = StepEntry(
            state=result.kind,
            idempotency_key=step_key,
            run_dir=result.evidence.run_dir,
            result=result.model_dump(mode="json"),
        )
        save()
        steps.append(_step_result(step, result, step_key))
        event(
            "step.end",
            step=step.id,
            kind=result.kind,
            code=getattr(result, "code", None),
            side_effect=result.side_effect,
            run_id=result.run_id,
            cached=result.cached,
        )
        if result.kind != "success":
            ending, ended_at = result, step
            break
        outputs[step.id] = dict(result.outputs)

    ran = {s.id for s in steps}
    for step in p.steps:
        if step.id not in ran:
            steps.append(
                StepResult(
                    id=step.id,
                    capability=step.capability.name,
                    version=step.version,
                    kind="not_run",
                    side_effect=refusal[2] if refusal and step is ended_at else "none",
                )
            )

    common: dict[str, Any] = {
        "workflow": wf.name,
        "workflow_version": wf.version,
        "workflow_run_id": run_id,
        "idempotency_key": key,
        "steps": steps,
        "run_dir": run_dir.as_posix(),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    side_effect = _fold([s.side_effect for s in steps])
    if refusal is not None:
        assert ended_at is not None
        result_wf = WorkflowResult(
            kind="failure",
            code=refusal[0],
            step_id=ended_at.id,
            message=refusal[1],
            side_effect=side_effect,
            **common,
        )
    elif ending is not None:
        assert ended_at is not None
        result_wf = WorkflowResult(
            kind=ending.kind,
            code=getattr(ending, "code", None) or getattr(ending, "reason", None),
            step_id=ended_at.id,
            message=_message(ended_at, ending),
            side_effect=side_effect,
            outputs=_outputs(p, request.inputs, outputs),
            resume_token=getattr(ending, "resume_token", None),
            operator_url=getattr(ending, "operator_url", None),
            detail=ending,
            **common,
        )
    else:
        result_wf = WorkflowResult(
            kind="success",
            side_effect=side_effect,
            outputs=_outputs(p, request.inputs, outputs),
            **common,
        )

    _write_json(run_dir / "result.json", result_wf.model_dump(mode="json"))
    event("workflow.end", kind=result_wf.kind, code=result_wf.code, step=result_wf.step_id)
    if result_wf.kind != "escalated":
        entry.result = result_wf.model_dump(mode="json")
    save()
    return result_wf


def _earlier_answer(
    prior: StepEntry | None, step: PlannedStep, runs_dir: Path
) -> ReplayResult | None:
    """What an earlier attempt of this request got from the step, if the step
    must not be started again for it: an escalated run (finished since by a
    person or ``cua resume``, or still waiting), or a committing run that
    was cut off. A finished step is answered by replay's own cache instead."""
    if prior is None or prior.state not in ("escalated", "started"):
        return None
    run_dir: Path | None = Path(prior.run_dir) if prior.run_dir else None
    if prior.state == "started":
        if step.idempotent or prior.idempotency_key is None:
            return None
        run_dir = _find_step_run(runs_dir, prior.idempotency_key)
    stored = _stored(run_dir) if run_dir is not None else None
    if stored is None and prior.result is not None:
        stored = RESULT.validate_python(prior.result)
    return stored.model_copy(update={"cached": True}) if stored is not None else None


def _find_step_run(runs_dir: Path, step_key: str | None) -> Path | None:
    return journal.find_run(runs_dir, step_key) if step_key else None


def _stored(run_dir: Path) -> ReplayResult | None:
    path = run_dir / "result.json"
    if not path.is_file():
        return None
    return RESULT.validate_json(path.read_text(encoding="utf-8"))


def _step_result(step: PlannedStep, result: ReplayResult, key: str | None) -> StepResult:
    return StepResult(
        id=step.id,
        capability=step.capability.name,
        version=step.version,
        kind=result.kind,
        code=getattr(result, "code", None) or getattr(result, "reason", None),
        side_effect=result.side_effect,
        outputs=dict(result.outputs),
        idempotency_key=key,
        run_id=result.run_id,
        run_dir=result.evidence.run_dir,
        cached=result.cached,
        duration_ms=result.duration_ms,
    )


def _outputs(p: Plan, inputs: dict[str, str], outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The workflow outputs that could be read; an absent one is left out."""
    out: dict[str, Any] = {}
    for name, text in p.workflow.outputs.items():
        ref = parse_ref(text)
        if ref.kind == "input" and ref.name in inputs:
            out[name] = inputs[ref.name]
        elif ref.kind == "output" and ref.step is not None:
            value = outputs.get(ref.step, {}).get(ref.name)
            if value is not None:
                out[name] = value
    return out


def _message(step: PlannedStep, result: ReplayResult) -> str:
    what = getattr(result, "message", "") or ""
    head = f"step {step.id!r} ({step.capability.name} v{step.version}) returned {result.kind}"
    if result.kind == "escalated":
        head += "; once it is resolved (`cua resume`), run the workflow again with the same key"
    return f"{head}: {what}" if what else head


def _fold(effects: list[SideEffect]) -> SideEffect:
    if "unknown" in effects:
        return "unknown"
    if "committed" in effects:
        return "committed"
    return "none"


def _refused(
    workflow: Workflow, request: WorkflowRequest, code: RefusalCode, message: str
) -> WorkflowResult:
    """Turned away before any step ran: certainly no side effect."""
    return WorkflowResult(
        kind="failure",
        code=code,
        message=message,
        workflow=workflow.name,
        workflow_version=workflow.version,
        idempotency_key=request.idempotency_key,
    )


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )
