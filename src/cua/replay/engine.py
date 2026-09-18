"""Deterministic replay: a capability, run step by step, with no model anywhere.

Per step, always in this order:

1. **find** the control: walk the locator ladder against a fresh observation
   until a rung names exactly one node and the step's ``wait_before`` holds —
   or the step's time runs out (``LOCATOR_UNRESOLVED`` / ``LOCATOR_AMBIGUOUS``);
2. **check** the action against the policy; a risky or ``approval: required``
   step needs the invocation's approval grant (``POLICY_BLOCKED`` without one);
3. **act**;
4. **land**: let the target settle, then look until one of these is true —
   an in-scope detector matches (hard → business → recoverable, in that
   order), or the step's ``expect_after`` *and* its checkpoint both hold, or
   the step's time runs out.

A hard detector ends the run as a ``Failure``; a business one as a
``BusinessOutcome``; a recoverable one runs its recoverer and either carries on
(``continue``), performs the step again (``retry_step``, only if the step says
``retry.allowed``), or signs on again from the entry and lets the resume-state
search decide where to pick up (``restart_from_last_checkpoint``).

When every step has landed, the outputs are read live off the screen, parsed
to their declared types, and the final checkpoint (``cp.done``) is checked.

Irreversible steps get the care the rest of the design exists for. The engine
never performs one twice: it is never retried, and a restart never resumes
before it once it has happened. If anything goes wrong after it was performed,
the step's ``side_effect_marker`` is checked on the screen: seen means
``side_effect: committed``, not seen means ``unknown`` — never ``none``.

Nothing here imports a model client or the discovery agent; a test proves it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict

from cua.artifact.schema import (
    DONE,
    Capability,
    Checkpoint,
    Detector,
    OutputSpec,
    Step,
    StepTimedOut,
)
from cua.evidence.logger import RunLog
from cua.policy.allowlist import Block, NeedsApproval, Policy, check, origin_allowed
from cua.policy.redaction import Redactor
from cua.replay import detectors as det_rules
from cua.replay.extract import ParseError, parse
from cua.replay.invocation import Invocation, bind, credential_field, fill
from cua.replay.recoverers import RecoveryLedger
from cua.replay.result import (
    BusinessOutcome,
    EscalationReason,
    Evidence,
    Failure,
    FailureCode,
    OutputValue,
    ReplayResult,
    SideEffect,
    Success,
)
from cua.replay.resume import find_resume_point, holds
from cua.replay.waits import Clock, Deadline, MonotonicClock, poll
from cua.secrets.resolver import Credential
from cua.surface.a11y import normalize
from cua.surface.conditions import (
    AllOf,
    TextPresent,
    ValidationMessagePresent,
    describe,
)
from cua.surface.locators import (
    Ambiguous,
    BBox,
    Ladder,
    LadderOutcome,
    Resolved,
    resolve_ladder,
)
from cua.surface.protocol import (
    CONTROL_ROLES,
    Action,
    ActionFailed,
    Click,
    Navigate,
    Node,
    Observation,
    PerceptionDrift,
    Press,
    ReadText,
    SelectOption,
    StaleRefError,
    Surface,
    SurfaceError,
    TypeText,
)
from cua.tenant import Tenant

EXCERPT_LINES = 40
REFIND_ATTEMPTS = 3
"""How many times a step looks for its control again when the screen changed
between finding it and acting on it. Nothing was done in between, so this is
not a retry of the step."""


class ReplayConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    step_timeout_s: float = 3.0
    """How long one step may take to land. The reference deployment answers in
    well under a second, so this is an order of magnitude of slack; a slower
    core raises it. A step that exceeds it is a timeout, which a capability may
    recover from by retrying — if, and only if, the step is retryable."""
    screenshots: bool = True
    """A masked screenshot of the screen every step landed on."""
    trust_low_confidence_rungs: bool = False
    """Act on a control found only by pixels. Off: a ladder that reaches its
    ``bbox`` rung has lost every way of naming the control, and clicking
    roughly the right place unattended is how the wrong button gets pressed."""


# --------------------------------------------------------------------------
# Internal outcomes
# --------------------------------------------------------------------------


class _Stop(Exception):
    """Ends the run from anywhere in a step. Control flow, not an error."""

    def __init__(self, result: ReplayResult) -> None:
        super().__init__(result.kind)
        self.result = result


class _Refind(Exception):
    """The screen moved between finding the control and acting on it."""


@dataclass(frozen=True)
class _Passed:
    observation: Observation
    checkpoint: Checkpoint | None


@dataclass(frozen=True)
class _Detected:
    detector: Detector
    observation: Observation


@dataclass(frozen=True)
class _TimedOut:
    observation: Observation
    expectation_held: bool
    """True when the step's own expectation came true and its checkpoint did
    not: the screen is the wrong one, not a slow one."""


_Landing = _Passed | _Detected | _TimedOut
_Mode = Literal["run", "recover"]


class ReplayEngine:
    def __init__(
        self,
        *,
        capability: Capability,
        surface: Surface,
        tenant: Tenant,
        policy: Policy,
        invocation: Invocation,
        credentials: Mapping[str, Credential],
        log: RunLog,
        config: ReplayConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.cap = capability
        self.surface = surface
        self.tenant = tenant
        self.policy = policy
        self.invocation = invocation
        self.inputs = dict(invocation.inputs)
        self.credentials = dict(credentials)
        self.log = log
        self.config = config or ReplayConfig()
        self.clock = clock or MonotonicClock()
        self.ev = surface.evaluator

        sensitive = [
            v
            for n, v in self.inputs.items()
            if n in capability.inputs and capability.inputs[n].sensitive
        ]
        self.redactor = Redactor(
            [*(v for c in self.credentials.values() for v in c.values()), *sensitive],
            sensitive_labels=policy.sensitive_labels,
        )
        self.masks: list[Ladder] = [
            *capability.redaction.screenshot_masks,
            *policy.screenshot_masks,
        ]
        self.ledger = RecoveryLedger(capability.recovery_limits, invocation.budget.max_recoveries)
        self.rungs: dict[str, str] = {}
        self.warnings: list[str] = []
        self.screenshots: list[str] = []
        self.outputs: dict[str, OutputValue] = {}
        self.committed_through: int | None = None
        """Index of the last irreversible step known to have happened."""
        self.passed: list[str] = []
        self._in_flight: str | None = None
        """An irreversible step that was performed and has not landed yet."""
        self._shots = 0
        self._started = self.clock.now()
        self._budget = Deadline(self.clock, invocation.budget.timeout_s)

    # -- the run -------------------------------------------------------------

    @property
    def entry_url(self) -> str:
        return self.cap.target.entry.pattern.replace(
            "{tenant.base_url}", self.tenant.base_url.rstrip("/")
        )

    def run(self) -> ReplayResult:
        self.log.event(
            "run.start",
            capability=self.cap.name,
            capability_id=self.cap.id,
            version=self.cap.version,
            content_sha256=self.cap.content_hash(),
            approval_state=self.cap.approval_state,
            tenant=self.tenant.id,
            entry=self.entry_url,
        )
        try:
            first = self.entry_url
            if self.invocation.inject:
                first += ("&" if "?" in first else "?") + f"inject={self.invocation.inject}"
            self._navigate(first)
            index = 0
            while index < len(self.cap.steps):
                index = self._step(index)
            result = self._done()
        except _Stop as stop:
            result = stop.result
        except SurfaceError as exc:
            # A fault the steps did not anticipate (the browser went away, a
            # frame detached mid-read). Still a typed result, never a crash.
            self.log.event("replay.surface_error", error=self.redactor.text(str(exc)))
            result = Failure(
                code="ACTION_FAILED",
                message=self.redactor.text(f"{type(exc).__name__}: {exc}"),
                side_effect=(
                    "committed"
                    if self.committed_through is not None
                    else "unknown"
                    if self._in_flight is not None
                    else "none"
                ),
                capability=self.cap.name,
                capability_version=self.cap.version,
            )
        return self._finish(result)

    def _finish(self, result: ReplayResult) -> ReplayResult:
        final = result.model_copy(
            update={
                "run_id": self.log.run_id,
                "capability": self.cap.name,
                "capability_version": self.cap.version,
                "idempotency_key": self.invocation.idempotency_key,
                "duration_ms": int((self.clock.now() - self._started) * 1000),
                "locator_rungs_used": dict(self.rungs),
                "recoveries": list(self.ledger.made),
                "warnings": list(self.warnings),
                "evidence": Evidence(
                    run_dir=self.log.dir.as_posix(),
                    screenshots=list(self.screenshots),
                    trace=result.evidence.trace,
                ),
            }
        )
        self.log.write_json("result.json", final)
        self.log.event("run.end", kind=final.kind, code=getattr(final, "code", None))
        return final

    # -- one step ------------------------------------------------------------

    def _step(self, index: int, mode: _Mode = "run") -> int:
        step = self.cap.steps[index]
        if self._budget.passed:
            self._within_budget(step, self.surface.observe())
        self.log.event("step.start", step=step.id, action=step.action, mode=mode)

        refinds = 0
        while True:
            node, screen = self._find(step)
            action = self._action(step, node)
            self._permit(step, action, screen)
            try:
                self._act(step, action)
            except _Refind:
                refinds += 1
                if refinds < REFIND_ATTEMPTS:
                    continue
                self._fail(
                    "ACTION_FAILED",
                    step,
                    expected="the screen to hold still long enough to act",
                    message="the control kept changing between being found and being acted on",
                    observation=self.surface.observe(),
                )

            landing = self._land(index, step, mode)
            while True:
                if isinstance(landing, _Passed):
                    self._passed(index, step, landing)
                    return index + 1
                if isinstance(landing, _Detected):
                    decision = self._on_detection(index, step, landing, mode)
                else:
                    decision = self._on_timeout(index, step, landing, mode)
                if isinstance(decision, int):
                    return decision  # a restart: the step to carry on from
                if decision == "land":
                    landing = self._land(index, step, mode)
                    continue
                # Retry. Look once more before doing the step again: a slow
                # screen that arrived during the backoff has landed, and
                # submitting again would only replace it.
                landing = self._land(index, step, mode, timeout_s=0.0)
                if isinstance(landing, _TimedOut):
                    break

    def _find(self, step: Step) -> tuple[Node | None, Observation]:
        """The step's control on a fresh screen, visible, named by exactly one
        trusted rung."""
        if step.target is None:
            return None, self.surface.observe()
        ladder = step.target
        wait = bind(step.wait_before, self.inputs) if step.wait_before else None
        last: list[LadderOutcome] = []

        def answer(obs: Observation) -> Resolved | None:
            outcome = resolve_ladder(ladder, obs, recording_env=self.cap.recording_env)
            last[:] = [outcome]
            if not isinstance(outcome, Resolved) or not self._trusted(ladder, outcome):
                return None
            if wait is not None and not self.ev.evaluate(wait, obs, target=outcome.ref):
                return None
            return outcome

        found, screen = poll(self.surface, self._deadline(), answer)
        if found is not None:
            self._note_rung(step.id, ladder, found)
            return found.node, screen

        outcome = last[0] if last else None
        if isinstance(outcome, Resolved) and self._trusted(ladder, outcome):
            self._fail(
                "TIMEOUT",
                step,
                expected=describe(wait) if wait else "the control to be ready",
                message=f"{outcome.node.label} was found but never became ready",
                observation=screen,
                escalate="STUCK",
            )
        code: FailureCode = (
            "LOCATOR_AMBIGUOUS" if isinstance(outcome, Ambiguous) else "LOCATOR_UNRESOLVED"
        )
        self._fail(
            code,
            step,
            expected=f"exactly one control for {_ladder_text(ladder)}",
            message=_attempts_text(outcome, ladder),
            observation=screen,
            escalate="STUCK",
        )

    def _trusted(self, ladder: Ladder, outcome: Resolved) -> bool:
        rung = ladder[outcome.rung_index]
        return (
            self.config.trust_low_confidence_rungs
            or not isinstance(rung, BBox)
            or rung.confidence != "low"
        )

    def _note_rung(self, key: str, ladder: Ladder, found: Resolved) -> None:
        self.rungs[key] = found.rung
        recorded = self.cap.provenance.locator_rungs_used.get(key)
        self.log.event(
            "locator.resolved",
            target=key,
            rung=found.rung,
            recorded=recorded,
            attempts=[a.model_dump() for a in found.attempts],
        )
        if found.slipped or (recorded is not None and recorded != found.rung):
            skipped = ", ".join(
                f"{a.rung} ({a.refused or f'{a.matches} matches'})" for a in found.attempts[:-1]
            )
            warning = (
                f"{key}: located by {found.rung}, recorded as {recorded or ladder[0].strategy}"
                + (f"; skipped {skipped}" if skipped else "")
            )
            self.warnings.append(warning)
            self.log.event("locator.slip", target=key, warning=warning)

    def _action(self, step: Step, node: Node | None) -> Action:
        ref = node.ref if node else None
        if step.action == "click":
            assert ref is not None
            return Click(ref=ref)
        if step.action == "type":
            assert ref is not None and step.value is not None
            return TypeText(ref=ref, text=self._expand(step, step.value))
        if step.action == "select":
            assert ref is not None and step.value is not None
            return SelectOption(ref=ref, value=self._expand(step, step.value))
        if step.action == "read":
            assert ref is not None
            return ReadText(ref=ref)
        return Press(key=step.key or "", ref=ref)

    def _expand(self, step: Step, value: str) -> str:
        """Inputs now; credentials at the last moment, and only here."""

        def credential(match: re.Match[str]) -> str:
            found = credential_field(match.group(1))
            if found is None:
                return match.group(0)
            name, field = found
            if name not in self.credentials:
                self._fail(
                    "POLICY_BLOCKED",
                    step,
                    expected=f"credential {name} to be resolved by the runner",
                    message=f"no credential {name} was supplied to this run",
                    observation=None,
                )
            return self.credentials[name].field(field)

        return re.sub(r"\$\{([^}]*)\}", credential, fill(value, self.inputs))

    def _permit(self, step: Step, action: Action, screen: Observation) -> None:
        decision = check(self.policy, action, screen)
        if isinstance(decision, Block):
            self.log.event("policy.block", step=step.id, reason=decision.reason)
            self._fail(
                "POLICY_BLOCKED",
                step,
                expected="an action the policy allows",
                message=decision.reason,
                observation=screen,
            )
        needs = step.approval == "required" or isinstance(decision, NeedsApproval)
        if not needs:
            return
        rule = decision.rule if isinstance(decision, NeedsApproval) else "step.approval"
        grant = self.invocation.approval
        if grant is None:
            self.log.event("policy.needs_approval", step=step.id, rule=rule)
            self._fail(
                "POLICY_BLOCKED",
                step,
                expected="an approval for this step (--approval-token), or a human to give one",
                message=f"{step.id} commits a change and needs approval; none was supplied",
                observation=screen,
                escalate="NEEDS_APPROVAL",
            )
        self.log.event(
            "policy.approved",
            step=step.id,
            rule=rule,
            approved_by=grant.approved_by,
            token_sha256=grant.token_sha256[:12],
        )

    def _act(self, step: Step, action: Action) -> None:
        if step.risk == "irreversible":
            self.log.event("irreversible.act", step=step.id)
            self._in_flight = step.id
        try:
            result = self.surface.act(action)
        except (PerceptionDrift, StaleRefError) as exc:
            # Raised before anything was done to the target.
            self.log.event("action.refind", step=step.id, reason=self.redactor.text(str(exc)))
            raise _Refind() from exc
        except (ActionFailed, SurfaceError) as exc:
            self._fail(
                "ACTION_FAILED",
                step,
                expected=f"{step.action} to be accepted",
                message=self.redactor.text(str(exc)),
                observation=self.surface.observe(),
                performed=step.risk == "irreversible",
                escalate="STUCK",
            )
        dialogs = self.redactor.dialogs(result.dialogs)
        self.log.event(
            "action.done",
            step=step.id,
            action=action.action,
            ms=result.duration_ms,
            dialogs=dialogs,
        )
        unexpected = [d for d in dialogs if not d.expected]
        if unexpected:
            d = unexpected[0]
            # An undeclared dialog was dismissed: for a confirm that is
            # "Cancel", so the step did not take effect.
            self._fail(
                "ACTION_FAILED",
                step,
                expected=f"{step.action} to take effect",
                message=f"it raised an unexpected {d.kind} ({d.message!r}), which was {d.answer}",
                observation=self.surface.observe(),
                escalate="STUCK",
            )

    def _land(
        self, index: int, step: Step, mode: _Mode, *, timeout_s: float | None = None
    ) -> _Landing:
        deadline = self._deadline(timeout_s)
        self.surface.settle(deadline.remaining)
        listening = det_rules.listening(self.cap, index)
        if mode == "recover":
            # A recovery does not recover from itself: only what ends a run
            # is listened for while one is in progress.
            listening = [d for d in listening if d.class_ != "recoverable"]
        expect = bind(step.expect_after, self.inputs) if step.expect_after else None
        checkpoint = self._checkpoint_after(step.id) if mode == "run" else None
        held = [False]

        def answer(obs: Observation) -> _Landing | None:
            detector = det_rules.first_match(listening, obs, self.ev, outputs=self.outputs)
            if detector is not None:
                return _Detected(detector, obs)
            ok = expect is None or self.ev.evaluate(
                expect, obs, outputs=self.outputs, target=self._self_ref(step, obs)
            )
            held[0] = ok
            if ok and (checkpoint is None or self._holds(checkpoint, obs)):
                return _Passed(obs, checkpoint)
            return None

        found, screen = poll(self.surface, deadline, answer)
        if isinstance(found, _Passed | _Detected):
            return found
        return _TimedOut(screen, expectation_held=held[0])

    def _self_ref(self, step: Step, obs: Observation) -> str | None:
        """``{target: self}`` after acting: the same control, found again by
        its ladder on the screen as it is now."""
        if step.target is None:
            return None
        outcome = resolve_ladder(step.target, obs, recording_env=self.cap.recording_env)
        return outcome.ref if isinstance(outcome, Resolved) else None

    def _passed(self, index: int, step: Step, landing: _Passed) -> None:
        if step.risk == "irreversible":
            self.committed_through = index
            self._in_flight = None
            self.log.event("irreversible.committed", step=step.id)
        if landing.checkpoint is not None:
            self.passed.append(landing.checkpoint.id)
            self.log.event("checkpoint.passed", checkpoint=landing.checkpoint.id, step=step.id)
        self.log.event("step.passed", step=step.id)
        self._snapshot(step.id)

    # -- what a landing turned out to be -------------------------------------

    def _on_detection(
        self, index: int, step: Step, landing: _Detected, mode: _Mode
    ) -> int | Literal["retry", "land"]:
        det, screen = landing.detector, landing.observation
        self.log.event("detector.matched", step=step.id, code=det.code, cls=det.class_)
        if det.class_ == "hard":
            self._fail(
                "APP_ERROR",
                step,
                expected=_expectation(step, self.inputs),
                message=f"{det.code}: {_match_text(det)}{_statuses(screen)}",
                observation=screen,
                performed=step.risk == "irreversible",
                code_detail=det.code,
            )
        if det.class_ == "business":
            self._business(step, det, screen)

        assert det.recover is not None
        self._within_budget(step, screen)
        refusal = self.ledger.refusal(step, det)
        if refusal is not None:
            self._fail(
                "RECOVERY_EXHAUSTED",
                step,
                expected=_expectation(step, self.inputs),
                message=f"{det.code} again, and no recovery is left: {refusal}",
                observation=screen,
                performed=step.risk == "irreversible",
                escalate="UNRECOVERABLE",
            )
        recover = det.recover
        if recover.then == "restart_from_last_checkpoint":
            return self._restart(index, step, det)
        if recover.run:
            self._run_recoverer(recover.run, step)
        if recover.then == "retry_step":
            if recover.backoff_s:
                self.surface.idle(min(recover.backoff_s, self._budget.remaining))
            self.ledger.record(step, det, "retry_step")
            self.log.event("recovery.retry", step=step.id, code=det.code)
            return "retry"
        self.ledger.record(step, det, recover.run or "continue")
        self.log.event("recovery.continue", step=step.id, code=det.code, ran=recover.run)
        return "land"

    def _on_timeout(
        self, index: int, step: Step, landing: _TimedOut, mode: _Mode
    ) -> Literal["retry"]:
        screen = landing.observation
        self._within_budget(step, screen, performed=step.risk == "irreversible")
        if landing.expectation_held:
            checkpoint = self._checkpoint_after(step.id)
            assert checkpoint is not None
            self._fail(
                "CHECKPOINT_FAILED",
                step,
                expected=describe(bind(AllOf(all_of=list(checkpoint.all_of)), self.inputs)),
                message=f"{step.id} arrived somewhere, but not at {checkpoint.id}",
                observation=screen,
                performed=step.risk == "irreversible",
                escalate="STUCK",
            )
        det = det_rules.on_timeout(self.cap) if mode == "run" else None
        refusal = self.ledger.refusal(step, det) if det is not None else None
        if det is None or refusal is not None:
            why = f" ({refusal})" if refusal else ""
            self._fail(
                "TIMEOUT",
                step,
                expected=_expectation(step, self.inputs),
                message=f"{step.id} did not land within {self.config.step_timeout_s}s{why}",
                observation=screen,
                performed=step.risk == "irreversible",
                escalate="STUCK",
            )
        assert det.recover is not None
        if det.recover.backoff_s:
            self.surface.idle(min(det.recover.backoff_s, self._budget.remaining))
        self.ledger.record(step, det, "retry_step")
        self.log.event("recovery.retry", step=step.id, code=det.code, cause="timeout")
        return "retry"

    def _business(self, step: Step, det: Detector, screen: Observation) -> NoReturn:
        payload: dict[str, Any] = {}
        message = ""
        if isinstance(det.match, ValidationMessagePresent):
            node = self.ev.validation_message(det.match, screen)
            if node is not None:
                message = self.redactor.text(node.text)
                if det.payload_from == "validation_message":
                    payload["message"] = message
                    field = self._field_about(node, screen)
                    if field is not None:
                        payload["field"] = field
        elif isinstance(det.match, TextPresent):
            message = self.redactor.text(_text_containing(det.match.text, screen) or det.match.text)
        partial = self._read_outputs(screen, optional_only=True)
        self._snapshot(f"{step.id}.{det.code.lower()}")
        self._stop(
            BusinessOutcome(
                code=det.code,
                payload=payload,
                message=message,
                outputs=partial,
                step_id=step.id,
                side_effect=self._side_effect(step, screen, performed=step.risk == "irreversible"),
                capability=self.cap.name,
                capability_version=self.cap.version,
            )
        )

    def _field_about(self, message: Node, screen: Observation) -> str | None:
        """Which input a validation message is about: the control whose label
        the message names (else the nearest), and the step that types an input
        into it."""
        controls = [
            n
            for n in screen.in_frame(message.frame)
            if n.role in CONTROL_ROLES and n.bbox is not None and message.bbox is not None
        ]
        said = normalize(message.text)
        named = [c for c in controls if c.near_text and normalize(c.near_text) in said]
        pool = named or controls
        if not pool or message.bbox is None:
            return None
        mx, my = message.bbox.center
        control = min(pool, key=lambda c: _distance(c, mx, my))
        for step in self.cap.steps:
            if step.target is None or step.value is None:
                continue
            placeholder = re.fullmatch(r"\$\{([a-z][a-z0-9_]*)\}", step.value)
            if placeholder is None:
                continue
            outcome = resolve_ladder(step.target, screen, recording_env=self.cap.recording_env)
            if isinstance(outcome, Resolved) and outcome.ref == control.ref:
                return placeholder.group(1)
        return None

    # -- recovery ------------------------------------------------------------

    def _run_recoverer(self, name: str, step: Step) -> None:
        recoverer = self.cap.recoverers[name]
        self.log.event("recovery.start", step=step.id, recoverer=name)
        for sub in recoverer.sub_flow:
            index = [s.id for s in self.cap.steps].index(sub)
            self._step(index, mode="recover")
        for action in recoverer.steps:
            target: Node | None = None
            if action.target is not None:
                ladder = action.target

                def answer(obs: Observation, ladder: Ladder = ladder) -> Resolved | None:
                    found = resolve_ladder(ladder, obs, recording_env=self.cap.recording_env)
                    return (
                        found
                        if isinstance(found, Resolved) and self._trusted(ladder, found)
                        else None
                    )

                found, screen = poll(self.surface, self._deadline(), answer)
                if found is None:
                    self._fail(
                        "RECOVERY_EXHAUSTED",
                        step,
                        expected=f"exactly one control for {_ladder_text(ladder)}",
                        message=f"recoverer {name} could not find its control",
                        observation=screen,
                    )
                target = found.node
            act: Action = (
                Click(ref=target.ref)
                if action.action == "click" and target is not None
                else Press(key=action.key or "", ref=target.ref if target else None)
            )
            self.surface.act(act)
            self.surface.settle(self._deadline().remaining)
        self.log.event("recovery.done", step=step.id, recoverer=name)

    def _restart(self, index: int, step: Step, det: Detector) -> int:
        """Start the session again from the entry, then let the screen say
        where the run has got back to."""
        assert det.recover is not None
        self.log.event("recovery.restart", step=step.id, code=det.code)
        self._navigate(self.entry_url)
        if det.recover.run:
            self._run_recoverer(det.recover.run, step)
        else:
            last = self._last_passed_step()
            for i in range(last + 1):
                self._step(i, mode="recover")

        not_before = self.committed_through + 1 if self.committed_through is not None else 0

        def answer(obs: Observation) -> int | None:
            point = find_resume_point(
                self.cap,
                obs,
                self.ev,
                inputs=self.inputs,
                outputs=self.outputs,
                not_before=not_before,
            )
            if point is None:
                return None
            resumed[0] = point.checkpoint
            return point.next_step

        resumed: list[str | None] = [None]
        nxt, screen = poll(self.surface, self._deadline(), answer)
        if nxt is None:
            self._fail(
                "RECOVERY_EXHAUSTED",
                step,
                expected="a checkpoint to hold after signing on again",
                message=f"{det.code}: signed on again, and no checkpoint holds",
                observation=screen,
                escalate="UNRECOVERABLE",
            )
        self.ledger.record(
            step, det, det.recover.run or "restart", resumed_after_checkpoint=resumed[0]
        )
        self.log.event("recovery.resumed", step=step.id, after=resumed[0], next=nxt)
        return nxt

    def _last_passed_step(self) -> int:
        ids = [s.id for s in self.cap.steps]
        for cp_id in reversed(self.passed):
            cp = next(c for c in self.cap.checkpoints if c.id == cp_id)
            if cp.after_step in ids:
                return ids.index(cp.after_step)
        return -1

    # -- outputs -------------------------------------------------------------

    def _done(self) -> ReplayResult:
        last = self.cap.steps[-1]
        required = {n: o for n, o in self.cap.outputs.items() if not o.optional}
        unresolved: list[str] = []

        def answer(obs: Observation) -> Observation | None:
            unresolved[:] = [
                n for n, o in required.items() if self._locate(o.extract.target, obs) is None
            ]
            return obs if not unresolved else None

        found, screen = poll(self.surface, self._deadline(), answer)
        if found is None:
            name = unresolved[0]
            ladder = required[name].extract.target
            self._fail(
                "EXTRACTION_FAILED",
                last,
                expected=f"output {name}: exactly one node for {_ladder_text(ladder)}",
                message=f"{name} could not be located on the final screen",
                observation=screen,
                performed=self.committed_through is not None,
            )
        outputs = self._read_outputs(screen, optional_only=False)
        self.outputs = outputs

        done = next((c for c in self.cap.checkpoints if c.after_step == DONE), None)
        if done is not None:
            if not self._holds(done, screen):
                self._fail(
                    "CHECKPOINT_FAILED",
                    last,
                    expected=describe(AllOf(all_of=list(done.all_of))),
                    message=f"the outputs were read, but {done.id} does not hold",
                    observation=screen,
                    performed=self.committed_through is not None,
                )
            self.passed.append(done.id)
            self.log.event("checkpoint.passed", checkpoint=done.id, step=DONE)
        self._snapshot("done")
        return Success(
            outputs=outputs,
            step_id=last.id,
            side_effect="committed" if self.committed_through is not None else "none",
            capability=self.cap.name,
            capability_version=self.cap.version,
        )

    def _locate(self, ladder: Ladder, obs: Observation) -> Resolved | None:
        found = resolve_ladder(ladder, obs, recording_env=self.cap.recording_env)
        return found if isinstance(found, Resolved) and self._trusted(ladder, found) else None

    def _read_outputs(self, screen: Observation, *, optional_only: bool) -> dict[str, OutputValue]:
        """Read outputs live off this screen. Required ones that cannot be
        read or parsed end the run; optional ones are left out, with a note."""
        out: dict[str, OutputValue] = {}
        for name, spec in self.cap.outputs.items():
            if optional_only and not spec.optional:
                continue
            found = self._locate(spec.extract.target, screen)
            if found is None:
                if spec.optional and not optional_only:
                    self.warnings.append(f"optional output {name} was not on the screen")
                continue
            try:
                text = self.surface.act(ReadText(ref=found.ref)).text or ""
                value = parse(self.redactor.text(text), spec)
            except (ParseError, SurfaceError) as exc:
                if spec.optional:
                    self.warnings.append(f"optional output {name} could not be read: {exc}")
                    continue
                self._fail(
                    "EXTRACTION_FAILED",
                    self.cap.steps[-1],
                    expected=f"output {name} as {_type_text(spec)}",
                    message=f"{name}: {self.redactor.text(str(exc))}",
                    observation=screen,
                    performed=self.committed_through is not None,
                )
            out[name] = value
            self._note_rung(f"outputs.{name}", spec.extract.target, found)
            self.log.event("output.extracted", name=name, rung=found.rung)
        return out

    # -- helpers -------------------------------------------------------------

    def _navigate(self, url: str) -> None:
        if not origin_allowed(self.policy, url):
            raise _Stop(
                Failure(
                    code="POLICY_BLOCKED",
                    message=f"the entry {url} is outside the allowed origins",
                    capability=self.cap.name,
                    capability_version=self.cap.version,
                )
            )
        self.log.event("navigate", url=url.split("?")[0])
        self.surface.act(Navigate(url=url))
        self.surface.settle(self.config.step_timeout_s)

    def _within_budget(self, step: Step, screen: Observation, *, performed: bool = False) -> None:
        """No recovery, and no retry, once the invocation's time is spent."""
        if self._budget.passed:
            self._fail(
                "TIMEOUT",
                step,
                expected=f"the run to finish within {self.invocation.budget.timeout_s}s",
                message=f"the invocation's time budget ran out at {step.id}",
                observation=screen,
                performed=performed,
            )

    def _deadline(self, seconds: float | None = None) -> Deadline:
        wanted = self.config.step_timeout_s if seconds is None else seconds
        return Deadline(self.clock, min(wanted, self._budget.remaining))

    def _checkpoint_after(self, step_id: str) -> Checkpoint | None:
        return next((c for c in self.cap.checkpoints if c.after_step == step_id), None)

    def _holds(self, checkpoint: Checkpoint, obs: Observation) -> bool:
        return holds(checkpoint, obs, self.ev, inputs=self.inputs, outputs=self.outputs)

    def _side_effect(
        self, step: Step, screen: Observation | None, *, performed: bool
    ) -> SideEffect:
        """What was committed, as far as can be known.

        An irreversible step that is known to have happened is ``committed``.
        One that was performed and then went wrong is ``committed`` only if
        its marker is on the screen; otherwise ``unknown`` — the engine did
        press the button, and cannot see what the button did.
        """
        if not performed:
            return "committed" if self.committed_through is not None else "none"
        marker = step.side_effect_marker
        if marker is not None and screen is not None:
            if self.ev.evaluate(bind(marker, self.inputs), screen, outputs=self.outputs):
                return "committed"
            fresh = self.surface.observe()
            if self.ev.evaluate(bind(marker, self.inputs), fresh, outputs=self.outputs):
                return "committed"
        return "unknown"

    def _snapshot(self, label: str) -> str | None:
        if not self.config.screenshots:
            return None
        raw = self.surface.observe(screenshot=True, masks=self.masks)
        screen = self.redactor.observation(raw)
        self._shots += 1
        paths = self.log.observation(self._shots, screen)
        self.log.event("observe", label=label, location=screen.location, **paths)
        shot = paths.get("screenshot")
        if shot:
            self.screenshots.append(shot)
        return shot

    def _fail(
        self,
        code: FailureCode,
        step: Step | None,
        *,
        expected: str,
        message: str,
        observation: Observation | None,
        performed: bool = False,
        escalate: EscalationReason | None = None,
        code_detail: str | None = None,
    ) -> NoReturn:
        side_effect = self._side_effect(step, observation, performed=performed) if step else "none"
        wanted = (
            escalate
            if escalate is not None and (step is None or step.on_fail == "escalate")
            else None
        )
        excerpt = self._excerpt(observation) if observation is not None else ""
        self.log.event(
            "replay.failed",
            code=code,
            detector=code_detail,
            step=step.id if step else None,
            expected=self.redactor.text(expected),
            message=self.redactor.text(message),
            side_effect=side_effect,
            escalation_reason=wanted,
        )
        if observation is not None:
            self._snapshot(f"{step.id if step else 'run'}.failed")
        self._stop(
            Failure(
                code=code,
                step_id=step.id if step else None,
                expected=self.redactor.text(expected),
                observed=excerpt,
                message=self.redactor.text(message),
                side_effect=side_effect,
                escalation_reason=wanted,
                capability=self.cap.name,
                capability_version=self.cap.version,
            )
        )

    def _stop(self, result: ReplayResult) -> NoReturn:
        raise _Stop(result)

    def _excerpt(self, observation: Observation) -> str:
        screen = self.redactor.observation(observation)
        frames = " ".join(
            f"[{f.name or 'top'}] {f.url}" + (f" ({f.status})" if f.status else "")
            for f in screen.frames
        )
        lines = screen.compact().splitlines()
        if len(lines) > EXCERPT_LINES:
            lines = [*lines[:EXCERPT_LINES], f"... {len(lines) - EXCERPT_LINES} more lines"]
        return "\n".join([frames, *lines]) if frames else "\n".join(lines)


# --------------------------------------------------------------------------
# Describing things in failure messages
# --------------------------------------------------------------------------


def _expectation(step: Step, inputs: dict[str, str]) -> str:
    if step.expect_after is None:
        return f"{step.id} to complete"
    return describe(bind(step.expect_after, inputs))


def _statuses(screen: Observation) -> str:
    failed = [
        f"{f.name or 'top'} returned HTTP {f.status}"
        for f in screen.frames
        if f.status is not None and f.status >= 400
    ]
    return f" ({'; '.join(failed)})" if failed else ""


def _match_text(det: Detector) -> str:
    match = det.match
    return "a timeout" if isinstance(match, StepTimedOut) else describe(match)


def _ladder_text(ladder: Ladder) -> str:
    parts = []
    for rung in ladder:
        if rung.strategy == "role_name":
            parts.append(f'role_name {rung.role} "{rung.name}"')
        elif rung.strategy == "near_text":
            parts.append(f'near_text "{rung.text}"' + (f" {rung.role}" if rung.role else ""))
        elif rung.strategy == "table_cell":
            parts.append(f'table_cell "{rung.row_contains}" x "{rung.column_header}"')
        else:
            parts.append(f"bbox ({rung.x:.0f},{rung.y:.0f} {rung.w:.0f}x{rung.h:.0f})")
    return " > ".join(parts)


def _attempts_text(outcome: LadderOutcome | None, ladder: Ladder) -> str:
    if outcome is None:
        return "the ladder was never tried"
    bits = []
    for attempt, rung in zip(outcome.attempts, ladder, strict=False):
        if attempt.refused:
            bits.append(f"{attempt.rung}: refused ({attempt.refused})")
        elif isinstance(rung, BBox) and attempt.matches == 1:
            bits.append(f"{attempt.rung}: 1 match, not trusted to act on unattended (pixels only)")
        else:
            bits.append(f"{attempt.rung}: {attempt.matches} matches")
    return "; ".join(bits)


def _type_text(spec: OutputSpec) -> str:
    return f"{spec.type} ({spec.extract.parse})"


def _text_containing(text: str, screen: Observation) -> str | None:
    wanted = normalize(text)
    return next((n.text for n in screen.nodes if n.text and wanted in normalize(n.text)), None)


def _distance(node: Node, x: float, y: float) -> float:
    assert node.bbox is not None
    cx, cy = node.bbox.center
    return float(((cx - x) ** 2 + (cy - y) ** 2) ** 0.5)
