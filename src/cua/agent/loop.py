"""Observe → decide → check → act, until the goal is reached or a limit is hit.

One turn is one action. The model is shown the screen (compact tree, and a
masked screenshot), picks exactly one tool, and the loop:

1. checks the call against the policy — blocked calls are refused and the
   model is told why; risky ones need approval, which in discovery is either
   ``--auto-approve-risky`` (logged loudly) or an escalation;
2. performs it through the ``Surface``, waiting for the screen to settle;
3. hands back the result together with the screen it left behind.

It ends in one of five ways. ``done`` — the agent named a ref for every
declared output and the loop read each one itself. ``escalated`` — the agent
said it was ``stuck``, the screen stopped changing (dead end), or a risky
action needed an approval nobody gave. With a handoff channel (``--handoff``)
each of these first becomes an intervention request on the operator console:
a person can approve the risky action, take the session over and hand it back
(the agent carries on from the screen they leave), or abort; the run ends
``escalated`` only if nobody answers. ``stopped`` — a step or time limit, or
an operator's abort. ``error`` — the model call itself failed. ``interrupted`` — something
outside the run's own logic cut it short (Ctrl+C, a crash in the surface);
the run directory still says so, and the exception carries on upwards.

Everything the model sees and everything the log records has been through
redaction first. Credentials reach the model only as placeholders and reach
the browser only at the moment of typing.
"""

from __future__ import annotations

import re
import sys
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field

from cua.agent import prompts
from cua.agent.goal import Goal, OutputSpec
from cua.agent.llm import (
    Decision,
    DecisionRequest,
    LLMClient,
    ToolResultTurn,
    Turn,
    UserTurn,
)
from cua.agent.script import Script, ScriptStep, dump_script
from cua.agent.stopping import StopLimits, Stopwatch
from cua.agent.tools import (
    AgentCall,
    ClickCall,
    DoneCall,
    PressCall,
    ReadCall,
    StuckCall,
    ToolInputError,
    TypeCall,
    parse_call,
    tool_definitions,
)
from cua.evidence.logger import RunLog, utc_now
from cua.policy.allowlist import Block, NeedsApproval, Policy, check
from cua.policy.redaction import Redactor
from cua.replay.handoff import Abort, HandBack, Handoff, HandoffRequest, Option, Unanswered
from cua.replay.result import EscalationReason
from cua.replay.result import Handoff as HandoffRecord
from cua.secrets.resolver import Credential
from cua.surface.locators import Ladder, Resolved, ladder_for, resolve_ladder
from cua.surface.protocol import (
    Action,
    Click,
    Navigate,
    Node,
    Observation,
    Press,
    ReadText,
    Surface,
    SurfaceError,
    TypeText,
)
from cua.tenant import Tenant

OutcomeKind = Literal["done", "escalated", "stopped", "error", "interrupted"]
_HANDED_BACK = (
    "A person had the session and handed it back; the screen may have changed since you "
    "last saw it."
)
EXCERPT_LINES = 40
_PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
_CREDENTIAL = re.compile(r"^credentials\.([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)$")


class DiscoveryConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    limits: StopLimits = Field(default_factory=StopLimits)
    screenshots: bool = True
    """Send a masked screenshot with every screen. The tree alone is usually
    enough on this class of app; the image is what catches an overlay."""
    auto_approve_risky: bool = False
    """Let risky actions through without an approval. For development before
    the handoff path exists; every use is logged as ``policy.auto_approved``
    and a run made with it is not evidence."""
    settle_timeout_s: float = 10.0


class ExtractedOutput(BaseModel):
    """One output as ``done`` delivered it, with what the recorder needs to
    find the same value again: the node and a ladder that names it."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: str
    ref: str
    value: str
    """As read off the screen, after redaction."""
    normalized: str
    node: Node
    ladder: Ladder


class DiscoveryOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: OutcomeKind
    reason: str | None = None
    """``STUCK``, ``DEAD_END``, ``NEEDS_APPROVAL``, ``MAX_STEPS``, ``TIMEOUT``,
    ``LLM_ERROR``; the exception's type name for ``interrupted``; ``None`` for
    ``done``."""
    message: str = ""
    outputs: dict[str, ExtractedOutput] = Field(default_factory=dict)
    steps: int = 0
    run_id: str
    run_dir: str
    duration_ms: int = 0
    handoffs: list[HandoffRecord] = Field(default_factory=list)
    human_assisted: bool = False
    """A person acted in the browser during the run. Its transcript is then not
    the whole story, and it is not recorded as a capability."""

    @property
    def exit_code(self) -> int:
        return {"done": 0, "escalated": 3, "stopped": 1, "error": 1, "interrupted": 130}[self.kind]


class _End(Exception):
    """Ends the run from anywhere in a turn. Control flow, not an error."""

    def __init__(self, kind: OutcomeKind, reason: str | None, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.reason = reason
        self.message = message


class DiscoveryLoop:
    def __init__(
        self,
        *,
        surface: Surface,
        llm: LLMClient,
        goal: Goal,
        tenant: Tenant,
        policy: Policy,
        credentials: dict[str, Credential],
        log: RunLog,
        config: DiscoveryConfig | None = None,
        entry_url: str | None = None,
        handoff: Handoff | None = None,
    ) -> None:
        self.surface = surface
        self.handoff = handoff
        self.handoffs: list[HandoffRecord] = []
        self.human_actions = 0
        self.llm = llm
        self.goal = goal
        self.tenant = tenant
        self.policy = policy
        self.credentials = credentials
        self.log = log
        self.config = config or DiscoveryConfig()
        self.entry_url = entry_url or tenant.url(goal.entry)
        self.redactor = Redactor.for_policy(
            policy, (v for c in credentials.values() for v in c.values())
        )
        log.scrub_with(self.redactor.text)
        self.watch = Stopwatch(self.config.limits)
        self.system = prompts.system_prompt(
            goal, tenant, policy, {name: c.field_names for name, c in credentials.items()}
        )
        self.tools = tool_definitions(goal.outputs)
        self.masks: list[Ladder] = list(policy.screenshot_masks)
        """Painted out of every screenshot. Grows: a control the agent types a
        credential into is masked from then on, so the picture the model and
        the evidence get agrees with the scrubbed tree."""
        self.transcript: list[Turn] = []
        self._calls: dict[str, str] = {}
        """Tool call id → tool name; a Gemini function response is keyed by name."""
        self.screen: Observation | None = None
        self.script: list[ScriptStep] = []
        self.outputs: dict[str, ExtractedOutput] = {}

    # -- the run -------------------------------------------------------------

    def run(self) -> DiscoveryOutcome:
        started = time.monotonic()
        self.log.event(
            "run.start",
            goal=self.goal.goal,
            entry=self.entry_url,
            provider=self.llm.provider,
            model=self.llm.model,
        )

        try:
            self.surface.act(Navigate(url=self.entry_url))
            self.surface.settle(self.config.settle_timeout_s)
            screen = self._look()
            self._write_run_json(screen)
            self.transcript.append(
                UserTurn(text=prompts.kickoff(screen), png=screen.screenshot_png)
            )
            while True:
                self._turn()
        except _End as end:
            outcome = self._outcome(end.kind, end.reason, end.message, started)
        except BaseException as exc:
            # Ctrl+C, or a fault nothing above anticipated. The run is over
            # either way, and a run directory with no ending cannot be told
            # apart from one still in progress — so record how it ended, then
            # let the exception carry on to whoever is waiting for it.
            message = self.redactor.text(str(exc) or repr(exc))
            self._finish(self._outcome("interrupted", type(exc).__name__, message, started))
            raise

        self._finish(outcome)
        return outcome

    def _outcome(
        self, kind: OutcomeKind, reason: str | None, message: str, started: float
    ) -> DiscoveryOutcome:
        return DiscoveryOutcome(
            kind=kind,
            reason=reason,
            message=message,
            outputs=self.outputs if kind == "done" else {},
            steps=self.watch.steps,
            run_id=self.log.run_id,
            run_dir=self.log.dir.as_posix(),
            duration_ms=int((time.monotonic() - started) * 1000),
            handoffs=list(self.handoffs),
            human_assisted=self.human_actions > 0,
        )

    def _finish(self, outcome: DiscoveryOutcome) -> None:
        self.log.write_json("result.json", outcome)
        if outcome.kind == "done":
            dump_script(
                Script(goal=self.goal.goal, steps=self.script), self.log.dir / "script.yaml"
            )
        self.log.event("run.end", kind=outcome.kind, reason=outcome.reason, message=outcome.message)

    def _turn(self) -> None:
        stop = self.watch.before_call()
        if stop == "DEAD_END":
            message = (
                f"the screen did not change across {self.config.limits.dead_end_repeats} actions"
            )
            if self.handoff is None:
                self._escalate("DEAD_END", message)
            self._ask("DEAD_END", "DEAD_END", message, ["take_control", "resume", "abort"])
            screen = self._look()
            self.transcript.append(
                UserTurn(
                    text=prompts.after_action(
                        _HANDED_BACK + " Carry on from this screen.", screen, self.watch.remaining
                    ),
                    png=screen.screenshot_png,
                )
            )
            return
        if stop is not None:
            raise _End("stopped", stop, f"{stop.lower().replace('_', ' ')} reached")

        assert self.screen is not None
        decision = self._decide()
        self.transcript.append(decision)

        if decision.tool_call is None:
            self.log.event("agent.no_tool", turn=self.watch.steps, text=decision.text)
            self.transcript.append(UserTurn(text="Call exactly one tool to take the next step."))
            return

        call_id = decision.tool_call.id
        self._calls[call_id] = decision.tool_call.name
        try:
            call = parse_call(decision.tool_call.name, decision.tool_call.input)
        except ToolInputError as exc:
            error = self.redactor.text(str(exc))
            self.log.event("agent.bad_call", turn=self.watch.steps, error=error)
            self._reply(call_id, f"Rejected: {error}", error=True)
            return

        self.log.event(
            "agent.decision",
            turn=self.watch.steps,
            tool=call.tool,
            input=self._scrub(call.model_dump(exclude={"tool", "reason"})),
            reason=self.redactor.text(call.reason),
            text=self.redactor.text(decision.text),
            target=self._label(getattr(call, "ref", None)),
        )
        self._handle(call_id, call)

    def _decide(self) -> Decision:
        assert self.screen is not None
        request = DecisionRequest(
            system=self.system,
            tools=self.tools,
            transcript=list(self.transcript),
            observation=self.screen,
        )
        try:
            decision = self.llm.decide(request)
        except Exception as exc:  # the SDK's error hierarchy is not ours to enumerate
            self.log.event(
                "model.error",
                turn=self.watch.steps,
                error=self.redactor.text(f"{type(exc).__name__}: {exc}"),
            )
            raise _End("error", "LLM_ERROR", f"model call failed: {type(exc).__name__}") from exc
        self.log.model_call(
            turn=self.watch.steps,
            provider=self.llm.provider,
            response_id=decision.response_id,
            model=decision.model,
            stop_reason=decision.stop_reason,
            usage=decision.usage,
        )
        return decision

    # -- tools ---------------------------------------------------------------

    def _handle(self, call_id: str, call: AgentCall) -> None:
        if isinstance(call, StuckCall):
            why = call.reason or "the agent could not continue"
            if self.handoff is None:
                self._escalate("STUCK", why)
            self._ask("STUCK", "STUCK", why, ["take_control", "resume", "abort"])
            self._reply_with_screen(call_id, _HANDED_BACK + " Carry on from this screen.")
            return
        if isinstance(call, DoneCall):
            self._done(call_id, call)
            return

        screen = self.screen
        assert screen is not None
        if call.ref is not None and screen.find(call.ref) is None:
            self._reply(call_id, f"{call.ref} is not on the current screen.", error=True)
            return

        try:
            action = self._action(call)
        except ValueError as exc:
            self._reply(call_id, str(exc), error=True)
            return

        permitted = self._permitted(call_id, action, screen)
        if permitted is None:
            return
        action = permitted

        node = screen.find(call.ref) if call.ref else None
        try:
            result = self.surface.act(action)
        except SurfaceError as exc:
            error = self.redactor.text(str(exc))
            self.log.event("action.failed", turn=self.watch.steps, error=error)
            self._reply_with_screen(call_id, f"The action failed: {error}", error=True)
            return

        self._record_step(call, node, screen)
        if isinstance(call, TypeCall) and node is not None and _PLACEHOLDER.search(call.text):
            ladder = ladder_for(node, screen)
            if ladder and ladder not in self.masks:
                self.masks.append(ladder)
                self.log.event("screenshot.mask_added", turn=self.watch.steps, target=node.label)
        if isinstance(call, ReadCall):
            text = self.redactor.text(result.text or "")
            self.log.event("action.read", turn=self.watch.steps, ref=call.ref, text=text)
            self._reply(call_id, f"{call.ref} reads {text!r}.")
            return

        dialogs = self.redactor.dialogs(result.dialogs)
        note = f"Done: {call.tool} on {node.label if node else 'the page'}."
        if dialogs:
            note += " " + " ".join(
                f"It raised a {d.kind} dialog ({d.message!r}), which was {d.answer}."
                for d in dialogs
            )
        self.log.event(
            "action.done",
            turn=self.watch.steps,
            tool=call.tool,
            ms=result.duration_ms,
            dialogs=dialogs,
        )
        self._reply_with_screen(call_id, note)

    def _action(self, call: ClickCall | TypeCall | PressCall | ReadCall) -> Action:
        if isinstance(call, ClickCall):
            return Click(ref=call.ref)
        if isinstance(call, TypeCall):
            return TypeText(ref=call.ref, text=self._expand(call.text))
        if isinstance(call, PressCall):
            return Press(key=call.key, ref=call.ref)
        return ReadText(ref=call.ref)

    def _permitted(self, call_id: str, action: Action, screen: Observation) -> Action | None:
        """The action to perform, or None (the agent has been told why not)."""
        decision = check(self.policy, action, screen)
        if isinstance(decision, Block):
            self.log.event("policy.block", turn=self.watch.steps, reason=decision.reason)
            self._reply(call_id, f"Blocked by policy: {decision.reason}", error=True)
            return None
        if isinstance(decision, NeedsApproval):
            if not self.config.auto_approve_risky:
                if self.handoff is None:
                    self._escalate("NEEDS_APPROVAL", decision.reason, rule=decision.rule)
                return self._approved(call_id, action, screen, decision)
            self.log.event(
                "policy.auto_approved",
                turn=self.watch.steps,
                rule=decision.rule,
                reason=decision.reason,
                warning="risky action approved by --auto-approve-risky; not valid as evidence",
            )
            print(
                f"WARNING: auto-approving risky action ({decision.rule}): {decision.reason}",
                file=sys.stderr,
            )
        return action

    def _approved(
        self, call_id: str, action: Action, screen: Observation, decision: NeedsApproval
    ) -> Action | None:
        """Put a risky action to a person; the same action on the same control
        if they approve it.

        Asking takes a fresh look at the screen (the request's screenshot),
        which spends every ref the agent was holding, and the person may have
        had the controls meanwhile. So the control is found again, by the
        ladder it was seen by, on the screen as it is now.
        """
        ref = getattr(action, "ref", None)
        node = screen.find(ref) if ref else None
        ladder = ladder_for(node, screen) if node is not None else None
        back = self._ask(
            "NEEDS_APPROVAL",
            "POLICY_BLOCKED",
            f"{decision.reason} (rule {decision.rule})",
            ["take_control", "resume", "approve", "abort"],
        )
        if not back.approved:
            self._reply_with_screen(
                call_id,
                "A person was asked to approve that action and handed back without "
                "approving it; it was not performed. Do not try it again. " + _HANDED_BACK,
                error=True,
            )
            return None
        self.log.event(
            "policy.approved",
            turn=self.watch.steps,
            rule=decision.rule,
            approved_by=back.by,
            via="console",
        )
        if ref is None:
            return action
        found = resolve_ladder(ladder, self.surface.observe()) if ladder else None
        if not isinstance(found, Resolved):
            self._reply_with_screen(
                call_id,
                "It was approved, but the control is no longer on the screen, so nothing was "
                "done. " + _HANDED_BACK,
                error=True,
            )
            return None
        return action.model_copy(update={"ref": found.ref})

    def _done(self, call_id: str, call: DoneCall) -> None:
        screen = self.screen
        assert screen is not None
        declared = {o.name: o for o in self.goal.outputs}
        problems: list[str] = []
        problems += [f"{n!r} is not a declared output" for n in call.outputs if n not in declared]
        problems += [
            f"{o.name!r} is missing"
            for o in self.goal.outputs
            if not o.optional and o.name not in call.outputs
        ]

        found: dict[str, ExtractedOutput] = {}
        for name, ref in call.outputs.items():
            spec = declared.get(name)
            if spec is None:
                continue
            node = screen.find(ref)
            if node is None:
                problems.append(f"{name}: {ref} is not on the current screen")
                continue
            try:
                raw = self.surface.act(ReadText(ref=ref)).text or ""
            except SurfaceError as exc:
                problems.append(f"{name}: could not read {ref}: {exc}")
                continue
            value = self.redactor.text(raw.strip())
            normalized, why = _normalize(value, spec)
            if why:
                problems.append(f"{name}: {ref} reads {value!r}, {why}")
                continue
            found[name] = ExtractedOutput(
                name=name,
                type=spec.type,
                ref=ref,
                value=value,
                normalized=normalized,
                node=node,
                ladder=ladder_for(node, screen, for_value=True),
            )

        if problems:
            self.log.event("agent.done_rejected", turn=self.watch.steps, problems=problems)
            self._reply(
                call_id,
                "done rejected: " + "; ".join(problems) + ". Fix these and call done again.",
                error=True,
            )
            return

        self.outputs = found
        self.script.append(
            ScriptStep(
                tool="done",
                outputs={n: o.ladder for n, o in found.items()},
                reason=call.reason,
            )
        )
        self.log.event(
            "agent.done",
            turn=self.watch.steps,
            outputs={
                n: {"ref": o.ref, "value": o.value, "normalized": o.normalized}
                for n, o in found.items()
            },
        )
        raise _End("done", None, call.reason or "goal reached")

    def _escalate(self, reason: str, message: str, **fields: Any) -> NoReturn:
        """Stop and ask for a human, with no channel to one: the request is
        logged with everything it would carry, and the run ends."""
        screen = self.screen
        self.log.event(
            "escalation.requested",
            turn=self.watch.steps,
            reason_code=reason,
            message=message,
            location=screen.location if screen else None,
            frames=[f.model_dump() for f in screen.frames] if screen else [],
            note="no handoff channel (--handoff); the run ends here",
            **fields,
        )
        raise _End("escalated", reason, message)

    def _ask(
        self, reason: EscalationReason, code: str, message: str, options: list[Option]
    ) -> HandBack:
        """Hand the session to a person and wait for it back. Returns the
        handback; an abort or no answer ends the run."""
        assert self.handoff is not None
        turn = self.watch.steps
        raw = self.surface.observe(screenshot=True, masks=self.masks)
        screen = self.redactor.observation(raw)
        paths = self.log.observation(turn, screen, suffix="-handoff")
        # Not an "observe": no decision was taken on this screen, and the
        # recorder reads each step against the screen it was decided on.
        self.log.event("escalation.screen", turn=turn, location=screen.location, **paths)
        message = self.redactor.text(message)
        ticket = self.handoff.open(
            HandoffRequest(
                step_id=f"turn {turn}",
                reason=reason,
                code=code,
                message=message,
                expected=self.goal.goal,
                observed=_excerpt(screen),
                screenshot=paths.get("screenshot"),
                options=options,
            )
        )
        self.log.event(
            "escalation.requested",
            turn=turn,
            request=ticket.request_id,
            reason_code=reason,
            message=message,
            intervention=ticket.intervention,
            operator_url=ticket.operator_url,
        )
        self.watch.pause()
        try:
            decision = self.handoff.wait(ticket)
        finally:
            self.watch.resume()
        count = self.handoff.human_actions(ticket)
        self.human_actions += count
        common: dict[str, Any] = {
            "request_id": ticket.request_id,
            "reason": reason,
            "step_id": f"turn {turn}",
            "human_actions_count": count,
        }
        if isinstance(decision, Unanswered):
            self.handoffs.append(HandoffRecord(**common, decision="unanswered"))
            raise _End("escalated", reason, f"{message} ({decision.why})")
        if isinstance(decision, Abort):
            self.handoffs.append(HandoffRecord(**common, decision="abort", decided_by=decision.by))
            self.log.event("handoff.aborted", turn=turn, by=decision.by, why=decision.why)
            why = f": {self.redactor.text(decision.why)}" if decision.why else ""
            raise _End("stopped", "ESCALATION_ABORTED", f"{decision.by} aborted the run{why}")
        kind: Literal["approve", "hand_back"] = "approve" if decision.approved else "hand_back"
        self.handoffs.append(
            HandoffRecord(
                **common, decision=kind, decided_by=decision.by, resumed_at=f"turn {turn + 1}"
            )
        )
        self.log.event(
            "handoff.handed_back",
            turn=turn,
            request=ticket.request_id,
            by=decision.by,
            decision=kind,
            human_actions=count,
        )
        self.handoff.resumed(ticket, checkpoint=None, next_step=f"turn {turn + 1}")
        self.watch.fresh_screen()
        return decision

    # -- screens and messages ------------------------------------------------

    def _look(self) -> Observation:
        raw = self.surface.observe(screenshot=self.config.screenshots, masks=self.masks)
        screen = self.redactor.observation(raw)
        paths = self.log.observation(self.watch.steps, screen)
        self.log.event(
            "observe", turn=self.watch.steps, location=screen.location, title=screen.title, **paths
        )
        self.screen = screen
        return screen

    def _reply_with_screen(self, call_id: str, note: str, *, error: bool = False) -> None:
        settled = self.surface.settle(self.config.settle_timeout_s)
        if not settled:
            note += " The screen was still loading when this was taken."
        screen = self._look()
        self.watch.screen_after_action(screen)
        text = prompts.after_action(note, screen, self.watch.remaining)
        self._tool_result(call_id, text, screen.screenshot_png, error=error)

    def _reply(self, call_id: str, note: str, *, error: bool = False) -> None:
        text = prompts.after_action(note, None, self.watch.remaining)
        self._tool_result(call_id, text, None, error=error)

    def _tool_result(self, call_id: str, text: str, png: bytes | None, *, error: bool) -> None:
        self.transcript.append(
            ToolResultTurn(
                call_id=call_id, tool=self._calls[call_id], text=text, png=png, is_error=error
            )
        )

    # -- helpers -------------------------------------------------------------

    def _expand(self, text: str) -> str:
        """Substitute credential placeholders at the last moment."""

        def one(match: re.Match[str]) -> str:
            found = _CREDENTIAL.match(match.group(1))
            if found is None:
                raise ValueError(
                    f"unknown placeholder ${{{match.group(1)}}}; only "
                    "${credentials.<name>.<field>} is substituted"
                )
            name, field = found.groups()
            credential = self.credentials.get(name)
            if credential is None or field not in credential.field_names:
                raise ValueError(f"there is no credential field {name}.{field}")
            return credential.field(field)

        return _PLACEHOLDER.sub(one, text)

    def _record_step(
        self,
        call: ClickCall | TypeCall | PressCall | ReadCall,
        node: Node | None,
        screen: Observation,
    ) -> None:
        target = (
            ladder_for(node, screen, for_value=isinstance(call, ReadCall))
            if node is not None
            else None
        )
        if node is not None and not target:
            self.log.event("script.unnamed_target", turn=self.watch.steps, node=node.label)
        # A secret the model typed literally, instead of by placeholder, is
        # masked here like anywhere else; the recorder then refuses the run.
        text = self.redactor.text(call.text) if isinstance(call, TypeCall) else None
        key = call.key if isinstance(call, PressCall) else None
        self.script.append(
            ScriptStep(tool=call.tool, target=target, text=text, key=key, reason=call.reason)
        )
        self.log.event(
            "step",
            turn=self.watch.steps,
            tool=call.tool,
            node=node.model_dump(mode="json") if node else None,
            ladder=[r.model_dump(mode="json") for r in target] if target else None,
            text=text,
            key=key,
        )

    def _scrub(self, value: Any) -> Any:
        """Redact every string inside a tool call's arguments before logging."""
        if isinstance(value, str):
            return self.redactor.text(value)
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        return value

    def _label(self, ref: str | None) -> str | None:
        if ref is None or self.screen is None:
            return None
        node = self.screen.find(ref)
        return node.label if node else None

    def _write_run_json(self, first: Observation) -> None:
        self.log.write_json(
            "run.json",
            {
                "run_id": self.log.run_id,
                "started_at": utc_now(),
                "goal": self.goal.model_dump(mode="json"),
                "entry_url": self.entry_url,
                "tenant": {
                    "id": self.tenant.id,
                    "app_family": self.tenant.app_family,
                    "base_url": self.tenant.base_url,
                },
                "credentials": {
                    name: {"ref": c.ref, "fields": c.field_names}
                    for name, c in self.credentials.items()
                },
                "provider": self.llm.provider,
                "model": self.llm.model,
                "limits": self.config.limits.model_dump(),
                "screenshots": self.config.screenshots,
                "auto_approve_risky": self.config.auto_approve_risky,
                # Pixel locators recorded in this run are valid only here.
                "recording_env": {"viewport": first.viewport.model_dump(), "dpr": 1.0},
            },
        )


def _normalize(value: str, spec: OutputSpec) -> tuple[str, str | None]:
    """Check a read value against its declared type. Returns (normalized, problem)."""
    if not value:
        return "", "which is empty"
    if spec.type == "string":
        return value, None
    text = value.replace("$", "").replace(",", "").strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return "", f"which is not a {spec.type}"
    if negative:
        number = -number
    if spec.type == "integer":
        if number != number.to_integral_value():
            return "", "which is not an integer"
        return str(int(number)), None
    return str(number), None


def _excerpt(screen: Observation) -> str:
    lines = screen.compact().splitlines()
    if len(lines) > EXCERPT_LINES:
        lines = [*lines[:EXCERPT_LINES], f"... {len(lines) - EXCERPT_LINES} more lines"]
    return "\n".join(lines)
