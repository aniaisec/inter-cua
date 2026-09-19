"""One invocation, end to end: everything around the engine.

In order, and each check before anything more expensive:

1. load the capability (a hand-edited file loads as a new draft);
2. **approval gate** — unattended replay runs only an ``approved`` capability.
   A draft is ``POLICY_BLOCKED`` unless the operator overrides it explicitly;
3. **input shape** — ``INPUT_INVALID`` with no browser started;
4. **approval token**, if one came — signature, expiry, and that it covers
   this capability, content, tenant and inputs. Any mismatch is
   ``POLICY_BLOCKED``: consent for something else is not consent;
5. **idempotency** — the same key and request returns the stored result;
6. **spent token** — a token that already went into a commit is refused, so
   one consent is one commit (a caller retrying after a lost answer retries
   with its idempotency key, and step 5 answers it);
7. credentials resolved from the tenant binding (held in memory only);
8. a fresh browser session, traced; the engine runs; a failed run keeps its
   trace, scrubbed of secrets. If the run reached a risky step, its token is
   spent, whatever the result.

With a handoff channel (``handoff=``), the session is also one a person can
be handed: the run gets a control record and a lease, and a fault the
capability escalates pauses for the operator console instead of failing. If
nobody picks the request up while this process waits, the result is
``escalated``; the browser is left running (a detached one outlives this
process) together with ``handoff_state.json``, and ``resume`` carries the run
on later from another process, given the resume token.

Returns a ``ReplayResult`` for every outcome a caller can act on. Raises
``InvocationError`` only for mistakes in how it was called — a missing file, an
unresolvable secret — which no result kind describes honestly.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, quote_plus

from pydantic import BaseModel, ConfigDict

from cua.artifact.schema import Capability
from cua.artifact.store import ArtifactError, open_capability
from cua.escalation.channel import (
    HandoffSettings,
    OperatorChannel,
    count_human_actions,
    waiter_alive,
)
from cua.escalation.controller import ControlStore, OperatorDecision
from cua.escalation.lease import LeasedSurface
from cua.escalation.requests import Queue
from cua.evidence import trace
from cua.evidence.logger import RUNS_DIR, RunLog, utc_now
from cua.policy import tokens
from cua.policy.allowlist import Policy
from cua.policy.redaction import Redactor
from cua.policy.tokens import Approval, SpentTokens, TokenRefused
from cua.replay.engine import ReplayConfig, ReplayEngine, SavedRun
from cua.replay.invocation import Budget, Invocation, request_summary, validate_inputs
from cua.replay.result import (
    RESULT,
    Failure,
    Handoff,
    IdempotencyCache,
    IdempotencyConflict,
    ReplayResult,
    SideEffect,
    fingerprint,
)
from cua.secrets.resolver import Credential, SecretError, resolve
from cua.surface.playwright_surface import BrowserProcess, PlaywrightSurface, kill_browser
from cua.surface.protocol import Surface
from cua.tenant import Tenant

SurfaceFactory = Callable[[], AbstractContextManager[PlaywrightSurface]]
HANDOFF_STATE = "handoff_state.json"
SESSION_FILE = "session.json"


class InvocationError(Exception):
    """The request could not be made at all (not a replay result)."""


@contextmanager
def launched(headed: bool | None = None, *, detached: bool = False) -> Iterator[PlaywrightSurface]:
    with PlaywrightSurface.launch(headed=headed, detached=detached) as surface:
        yield surface


class SessionRecord(BaseModel):
    """The live browser a waiting run left behind."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cdp_url: str
    target_id: str | None = None
    pid: int | None = None
    profile: str | None = None


class ResumeContext(BaseModel):
    """``handoff_state.json``: how to carry an escalated run on elsewhere.

    Sensitive inputs are kept as hashes only; ``resume`` needs them supplied
    again, and checks them against these."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    saved: SavedRun
    capability_path: str
    content_sha256: str
    tenant: Tenant
    policy: Policy
    inputs: dict[str, str]
    sensitive: list[str]
    idempotency_key: str | None = None
    budget: Budget
    config: ReplayConfig
    allow_draft: bool = False
    handoff: HandoffSettings
    session: SessionRecord


def replay(
    path: Path,
    *,
    tenant: Tenant,
    policy: Policy,
    invocation: Invocation,
    runs_dir: Path = RUNS_DIR,
    allow_draft: bool = False,
    config: ReplayConfig | None = None,
    surface: SurfaceFactory | None = None,
    environ: dict[str, str] | None = None,
    handoff: HandoffSettings | None = None,
) -> ReplayResult:
    """``handoff``: attach the operator channel (``cua replay --handoff``).
    Without it, a fault that should go to a person is returned as a
    ``Failure`` naming the escalation it replaced."""
    try:
        loaded = open_capability(path)
    except ArtifactError as exc:
        raise InvocationError(str(exc)) from None
    cap = loaded.capability

    if cap.approval_state != "approved" and not allow_draft:
        edited = " (it was edited by hand after it was saved)" if loaded.edited_outside else ""
        return _refused(
            cap,
            invocation,
            "POLICY_BLOCKED",
            f"{cap.name} v{cap.version} is a draft{edited}; unattended replay runs only an "
            f"approved capability. Review it with `cua describe {path.as_posix()}`, then "
            "`cua approve`.",
        )
    if cap.target.app_family != tenant.app_family:
        return _refused(
            cap,
            invocation,
            "POLICY_BLOCKED",
            f"{cap.name} is for app family {cap.target.app_family!r}; tenant {tenant.id!r} "
            f"runs {tenant.app_family!r}",
        )

    problems = validate_inputs(cap, invocation.inputs)
    if problems:
        return _refused(cap, invocation, "INPUT_INVALID", "; ".join(problems))

    approval: Approval | None = None
    if invocation.approval is not None:
        try:
            approval = _verified(cap, tenant, invocation, environ)
        except TokenRefused as exc:
            return _refused(cap, invocation, "POLICY_BLOCKED", f"approval refused: {exc}")

    cache = IdempotencyCache(runs_dir) if invocation.idempotency_key else None
    request = fingerprint(cap.name, cap.version, invocation.inputs)
    if cache is not None and invocation.idempotency_key is not None:
        try:
            cached = cache.get(invocation.idempotency_key, request)
        except IdempotencyConflict as exc:
            return _refused(cap, invocation, "INPUT_INVALID", str(exc))
        if cached is not None:
            return cached

    spent = SpentTokens(runs_dir)
    if approval is not None:
        used_by = spent.spent_by(approval)
        if used_by is not None:
            return _refused(
                cap,
                invocation,
                "POLICY_BLOCKED",
                f"approval refused: this token was already used by {used_by}; one consent "
                "covers one commit. Retry with the same --idempotency-key to get that run's "
                "result, or ask for new consent.",
            )

    credentials = _credentials(cap, tenant, environ)
    result = _run(
        cap,
        path=path,
        tenant=tenant,
        policy=policy,
        invocation=invocation,
        approval=approval,
        spent=spent,
        credentials=credentials,
        runs_dir=runs_dir,
        allow_draft=allow_draft,
        config=config or ReplayConfig(),
        surface=surface or (lambda: launched(detached=handoff is not None)),
        handoff=handoff,
    )
    if cache is not None and invocation.idempotency_key is not None:
        cache.put(invocation.idempotency_key, request, result)
    return result


def _run(
    cap: Capability,
    *,
    path: Path,
    tenant: Tenant,
    policy: Policy,
    invocation: Invocation,
    approval: Approval | None,
    spent: SpentTokens,
    credentials: dict[str, Credential],
    runs_dir: Path,
    allow_draft: bool,
    config: ReplayConfig,
    surface: SurfaceFactory,
    handoff: HandoffSettings | None,
) -> ReplayResult:
    log = RunLog.create(runs_dir)
    log.write_json(
        "run.json",
        {
            "run_id": log.run_id,
            "kind": "replay",
            "started_at": utc_now(),
            "capability": {
                "id": cap.id,
                "name": cap.name,
                "version": cap.version,
                "approval_state": cap.approval_state,
                "approved_by": cap.approved_by,
                "content_sha256": cap.content_hash(),
            },
            "tenant": {
                "id": tenant.id,
                "app_family": tenant.app_family,
                "base_url": tenant.base_url,
            },
            "request": request_summary(invocation, cap),
            "allow_draft": allow_draft,
            "handoff": handoff.model_dump() if handoff else None,
        },
    )
    if allow_draft and cap.approval_state != "approved":
        log.event("policy.draft_override", warning="a draft capability was replayed by override")
        print(
            f"WARNING: replaying draft {cap.name} v{cap.version} by --allow-draft override",
            file=sys.stderr,
        )

    def resume_context(engine: ReplayEngine, live: PlaywrightSurface) -> ResumeContext | None:
        if engine.suspended is None or handoff is None:
            return None
        sensitive = sorted(n for n in invocation.inputs if cap.inputs[n].sensitive)
        return ResumeContext(
            saved=engine.suspended,
            capability_path=path.as_posix(),
            content_sha256=cap.content_hash(),
            tenant=tenant,
            policy=policy,
            inputs={
                n: (_hash_input(v) if n in sensitive else v) for n, v in invocation.inputs.items()
            },
            sensitive=sensitive,
            idempotency_key=invocation.idempotency_key,
            budget=invocation.budget,
            config=config,
            allow_draft=allow_draft,
            handoff=handoff,
            session=_session_record(live),
        )

    return _execute(
        cap,
        log=log,
        tenant=tenant,
        policy=policy,
        invocation=invocation,
        approval=approval,
        spent=spent,
        credentials=credentials,
        runs_dir=runs_dir,
        config=config,
        surface=surface,
        handoff=handoff,
        fresh_control=True,
        drive=lambda engine: engine.run(),
        resume_context=resume_context,
    )


def _execute(
    cap: Capability,
    *,
    log: RunLog,
    tenant: Tenant,
    policy: Policy,
    invocation: Invocation,
    approval: Approval | None,
    spent: SpentTokens,
    credentials: dict[str, Credential],
    runs_dir: Path,
    config: ReplayConfig,
    surface: SurfaceFactory,
    handoff: HandoffSettings | None,
    fresh_control: bool,
    drive: Callable[[ReplayEngine], ReplayResult],
    resume_context: Callable[[ReplayEngine, PlaywrightSurface], ResumeContext | None],
) -> ReplayResult:
    """One session, one engine: shared by a first run and a resumed one."""
    secrets = [v for c in credentials.values() for v in c.values()]
    secrets += [v for n, v in invocation.inputs.items() if cap.inputs[n].sensitive]
    with surface() as live:
        context = live.page.context
        trace.start(context)
        driven: Surface = live
        channel: OperatorChannel | None = None
        control: ControlStore | None = None
        if handoff is not None:
            control = ControlStore(log.dir)
            if fresh_control:
                control.start(log.run_id)
            channel = OperatorChannel(
                log=log,
                runs_dir=runs_dir,
                control=control,
                session=live.expose,
                kind="replay",
                capability=cap.name,
                capability_version=cap.version,
                tenant=tenant.id,
                settings=handoff,
            )
            # Every action the engine takes asks the lease first.
            driven = LeasedSurface(live, control.lease)
            log.write_json(SESSION_FILE, _session_record(live))
        engine = ReplayEngine(
            capability=cap,
            surface=driven,
            tenant=tenant,
            policy=policy,
            invocation=invocation,
            credentials=credentials,
            log=log,
            approval=approval,
            config=config,
            handoff=channel,
        )
        # An interruption (Ctrl+C, a crash) is written by the engine as a
        # ``Failure INTERRUPTED`` with the side effect it can vouch for, and
        # then re-raised; the browser is closed on the way out.
        try:
            result = drive(engine)
        finally:
            if approval is not None and engine.side_effect_so_far() != "none":
                spent.spend(approval, log.run_id)
                log.event("approval.spent", token_sha256=approval.token_sha256[:12])

        saved = resume_context(engine, live) if result.kind == "escalated" else None
        kept: Path | None = None
        if saved is not None:
            # The session is the person's now: leave it up, and leave what
            # ``resume`` needs beside it. The trace so far is discarded.
            log.write_json(HANDOFF_STATE, saved)
            if live.process is None:
                log.event(
                    "session.not_detached",
                    warning="this browser closes with this process; `cua resume` will not find it",
                )
            live.keep_open()
            try:
                trace.stop(context, keep_as=None, redactor=Redactor([]))
            except Exception:
                pass
        else:
            if control is not None:
                control.end(result.kind)
            keep = log.dir / trace.TRACE_NAME if result.kind == "failure" else None
            kept = trace.stop(
                context, keep_as=keep, redactor=Redactor.for_policy(policy, _encodings(secrets))
            )

    if kept is not None:
        evidence = result.evidence.model_copy(
            update={"trace": kept.relative_to(log.dir).as_posix()}
        )
        result = result.model_copy(update={"evidence": evidence})
        log.write_json("result.json", result)
    return result


# --------------------------------------------------------------------------
# Carrying an escalated run on: `cua resume`
# --------------------------------------------------------------------------


def resume(
    resume_token: str,
    *,
    runs_dir: Path = RUNS_DIR,
    by: str = "cua-resume",
    resume_at: str | None = None,
    inputs: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
    surface: Callable[[ResumeContext], AbstractContextManager[PlaywrightSurface]] | None = None,
    wait_s: float | None = None,
) -> ReplayResult:
    """Carry on a run that returned ``escalated``, in this process.

    The resume token names the request. If the person has not handed back on
    the console yet, calling this *is* the handback (by ``by``, optionally at
    ``resume_at``); either way the engine then runs the resume-state search on
    the live session and carries on — or asks again. A run that has already
    ended returns its stored result.
    """
    queue = Queue(runs_dir)
    entry = queue.by_token(resume_token)
    if entry is None:
        raise InvocationError("no escalated run has this resume token")
    run_dir = queue.run_dir(entry)
    try:
        ctx = ResumeContext.model_validate_json(
            (run_dir / HANDOFF_STATE).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise InvocationError(f"{run_dir.as_posix()} cannot be resumed: {exc}") from None

    control = ControlStore(run_dir)
    record = control.read()
    stored = _stored_result(run_dir)
    if record.ended is not None and record.ended != "escalated" and stored is not None:
        return stored.model_copy(update={"cached": True})
    if record.request_id != entry.request_id and stored is not None:
        return stored.model_copy(update={"cached": True})  # asked again since: that answer
    if waiter_alive(run_dir):
        raise InvocationError(
            "the run's own process is still waiting on this request; hand back on the "
            "operator console instead"
        )

    try:
        loaded = open_capability(Path(ctx.capability_path))
    except ArtifactError as exc:
        raise InvocationError(str(exc)) from None
    cap = loaded.capability
    if cap.content_hash() != ctx.content_sha256:
        raise InvocationError(
            f"{ctx.capability_path} has changed since the run started; a run is carried on "
            "only with the capability it began with"
        )
    values = _restore_inputs(ctx, inputs or {})

    if record.state == "PAUSED":
        record = control.transition(
            "HUMAN_IN_CONTROL", by=by, expect_request=entry.request_id, note="cua resume"
        )
    if record.state == "HUMAN_IN_CONTROL":
        control.transition(
            "RESUMING",
            by=by,
            expect_request=entry.request_id,
            decision=OperatorDecision(kind="hand_back", by=by, resume_at=resume_at),
            note="handed back with cua resume",
        )

    log = RunLog(run_dir)
    handoff = ctx.handoff if wait_s is None else ctx.handoff.model_copy(update={"wait_s": wait_s})
    invocation = Invocation(
        inputs=values,
        idempotency_key=ctx.idempotency_key,
        budget=ctx.budget,
    )
    credentials = _credentials(cap, ctx.tenant, environ)
    factory = surface or _attached
    try:
        result = _execute(
            cap,
            log=log,
            tenant=ctx.tenant,
            policy=ctx.policy,
            invocation=invocation,
            approval=None,  # consent is never carried over; the console gives it again
            spent=SpentTokens(runs_dir),
            credentials=credentials,
            runs_dir=runs_dir,
            config=ctx.config,
            surface=lambda: factory(ctx),
            handoff=handoff,
            fresh_control=False,
            drive=lambda engine: engine.resume(ctx.saved),
            resume_context=lambda engine, live: _next_context(ctx, engine, live),
        )
    except SessionGone as exc:
        result = finalize_without_session(run_dir, ctx, why=str(exc), control=control)

    if ctx.idempotency_key is not None:
        request = fingerprint(cap.name, cap.version, values)
        IdempotencyCache(runs_dir).put(ctx.idempotency_key, request, result)
    return result


class SessionGone(Exception):
    """The browser a waiting run left behind is not there any more."""


@contextmanager
def _attached(ctx: ResumeContext) -> Iterator[PlaywrightSurface]:
    s = ctx.session
    process = BrowserProcess(s.pid, s.cdp_url, Path(s.profile)) if s.pid and s.profile else None
    try:
        live = PlaywrightSurface.attach(s.cdp_url, target_id=s.target_id, process=process)
    except Exception as exc:
        if process is not None:
            process.kill()
        raise SessionGone(f"the live session at {s.cdp_url} is gone: {exc}") from None
    with live:
        yield live


def _next_context(
    ctx: ResumeContext, engine: ReplayEngine, live: PlaywrightSurface
) -> ResumeContext | None:
    if engine.suspended is None:
        return None
    return ctx.model_copy(update={"saved": engine.suspended, "session": _session_record(live)})


def finalize_without_session(
    run_dir: Path,
    ctx: ResumeContext,
    *,
    why: str,
    control: ControlStore | None = None,
    by: str | None = None,
) -> ReplayResult:
    """End a waiting run whose session will not be carried on (aborted with no
    process to pick it up, or the browser gone), from the files alone.

    Nothing can be observed any more, so the side effect is what the run knew,
    made ``unknown`` if a person had the controls and an irreversible step was
    still ahead of the run: what they did cannot be vouched for."""
    control = control or ControlStore(run_dir)
    saved = ctx.saved
    failure = saved.escalation
    request_id = saved.ticket.request_id
    count = count_human_actions(run_dir, request_id)
    side_effect: SideEffect = failure.side_effect
    cap = open_capability(Path(ctx.capability_path)).capability
    floor = saved.committed_through + 1 if saved.committed_through is not None else 0
    ahead = any(s.risk == "irreversible" for s in cap.steps[floor:])
    if side_effect == "none" and count > 0 and ahead:
        side_effect = "unknown"
    record = control.read()
    decided = record.decision.by if record.decision is not None else (by or "system")
    if record.state in ("PAUSED", "HUMAN_IN_CONTROL"):
        control.transition(
            "ABORTED",
            by=decided,
            expect_request=request_id,
            decision=OperatorDecision(kind="abort", by=decided, why=why),
            note=why,
        )
    result = failure.model_copy(
        update={
            "code": "ESCALATION_ABORTED",
            "message": f"{why} ({failure.escalation_reason} at {failure.step_id}, after "
            f"{failure.code}; decided by {decided})",
            "side_effect": side_effect,
            "run_id": run_dir.name,
            "handoffs": [
                *saved.handoffs,
                Handoff(
                    request_id=request_id,
                    reason=failure.escalation_reason or "STUCK",
                    step_id=failure.step_id,
                    decision="abort",
                    decided_by=decided,
                    human_actions_count=count,
                ),
            ],
            "recoveries": saved.recoveries,
            "warnings": saved.warnings,
            "locator_rungs_used": saved.rungs,
            "evidence": failure.evidence.model_copy(
                update={
                    "run_dir": run_dir.as_posix(),
                    "screenshots": saved.screenshots,
                    "intervention": saved.ticket.intervention,
                }
            ),
        }
    )
    log = RunLog(run_dir)
    log.event("handoff.aborted", request=request_id, by=decided, why=why, human_actions=count)
    log.write_json("result.json", result)
    log.event("run.end", kind=result.kind, code=result.code)
    control.end(result.kind)
    if ctx.session.pid is not None:
        kill_browser(ctx.session.pid, Path(ctx.session.profile) if ctx.session.profile else None)
    return result


def _restore_inputs(ctx: ResumeContext, given: dict[str, str]) -> dict[str, str]:
    values = {n: v for n, v in ctx.inputs.items() if n not in ctx.sensitive}
    for name in ctx.sensitive:
        if name not in given:
            raise InvocationError(
                f"{name!r} is a sensitive input and was not kept; supply it again "
                f"(--input {name}=...)"
            )
        if _hash_input(given[name]) != ctx.inputs[name]:
            raise InvocationError(f"{name!r} is not the value the run was started with")
        values[name] = given[name]
    return values


def _hash_input(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stored_result(run_dir: Path) -> ReplayResult | None:
    path = run_dir / "result.json"
    if not path.is_file():
        return None
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    return RESULT.validate_python(data)


def _session_record(live: PlaywrightSurface) -> SessionRecord:
    handle = live.expose()
    process = live.process
    return SessionRecord(
        cdp_url=handle.cdp_url,
        target_id=handle.target_id,
        pid=process.pid if process else None,
        profile=process.profile.as_posix() if process else None,
    )


def _verified(
    cap: Capability, tenant: Tenant, invocation: Invocation, environ: dict[str, str] | None
) -> Approval:
    assert invocation.approval is not None
    grant = invocation.approval
    approval = tokens.verify(
        grant.token,
        cap,
        tenant,
        invocation.inputs,
        key=tokens.signing_key(tenant, environ=environ),
    )
    if grant.approved_by is not None and grant.approved_by != approval.approved_by:
        raise TokenRefused(
            f"the token was signed for {approval.approved_by!r}, not {grant.approved_by!r}"
        )
    return approval


def _credentials(
    cap: Capability, tenant: Tenant, environ: dict[str, str] | None
) -> dict[str, Credential]:
    out: dict[str, Credential] = {}
    for name, spec in cap.credentials.items():
        ref = spec.ref.replace("{tenant.id}", tenant.id)
        try:
            credential = resolve(ref, tenant, environ=environ)
        except SecretError as exc:
            raise InvocationError(f"credential {name}: {exc}") from None
        missing = [f for f in spec.fields if f not in credential.field_names]
        if missing:
            raise InvocationError(f"credential {name} ({ref}) has no field(s) {missing}")
        out[name] = credential
    return out


def _encodings(secrets: list[str]) -> list[str]:
    """Each secret as it may appear inside a trace: raw, form-encoded, URL
    encoded, and JSON-escaped."""
    out: set[str] = set()
    for s in secrets:
        if s:
            out.update({s, quote_plus(s), quote(s, safe=""), json.dumps(s)[1:-1]})
    return sorted(out)


def _refused(
    cap: Capability,
    invocation: Invocation,
    code: Literal["POLICY_BLOCKED", "INPUT_INVALID"],
    message: str,
) -> Failure:
    """Turned away before any browser started: certainly no side effect."""
    return Failure(
        code=code,
        message=message,
        side_effect="none",
        capability=cap.name,
        capability_version=cap.version,
        idempotency_key=invocation.idempotency_key,
    )
