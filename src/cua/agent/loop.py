"""Observe → decide → check → act, until the goal is reached or a limit is hit.

One turn is one action. The model is shown the screen (compact tree, and a
masked screenshot), picks exactly one tool, and the loop:

1. checks the call against the policy — blocked calls are refused and the
   model is told why; risky ones need approval, which in discovery is either
   ``--auto-approve-risky`` (logged loudly) or an escalation;
2. performs it through the ``Surface``, waiting for the screen to settle;
3. hands back the result together with the screen it left behind.

It ends in one of four ways. ``done`` — the agent named a ref for every
declared output and the loop read each one itself. ``escalated`` — the agent
said it was ``stuck``, the screen stopped changing (dead end), or a risky
action needed an approval nobody gave; M6 routes these to a human, and until
then the run ends there with the request logged. ``stopped`` — a step or time
limit. ``error`` — the model call itself failed.

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
from cua.secrets.resolver import Credential
from cua.surface.locators import Ladder, ladder_for
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

OutcomeKind = Literal["done", "escalated", "stopped", "error"]
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
    ``LLM_ERROR``; ``None`` for ``done``."""
    message: str = ""
    outputs: dict[str, ExtractedOutput] = Field(default_factory=dict)
    steps: int = 0
    run_id: str
    run_dir: str
    duration_ms: int = 0

    @property
    def exit_code(self) -> int:
        return {"done": 0, "escalated": 3, "stopped": 1, "error": 1}[self.kind]


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
    ) -> None:
        self.surface = surface
        self.llm = llm
        self.goal = goal
        self.tenant = tenant
        self.policy = policy
        self.credentials = credentials
        self.log = log
        self.config = config or DiscoveryConfig()
        self.entry_url = entry_url or tenant.url(goal.entry)
        self.redactor = Redactor(
            (v for c in credentials.values() for v in c.values()),
            sensitive_labels=policy.sensitive_labels,
        )
        self.watch = Stopwatch(self.config.limits)
        self.system = prompts.system_prompt(
            goal, tenant, policy, {name: c.field_names for name, c in credentials.items()}
        )
        self.tools = tool_definitions(goal.outputs)
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
            outcome = DiscoveryOutcome(
                kind=end.kind,
                reason=end.reason,
                message=end.message,
                outputs=self.outputs if end.kind == "done" else {},
                steps=self.watch.steps,
                run_id=self.log.run_id,
                run_dir=self.log.dir.as_posix(),
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        self.log.write_json("result.json", outcome)
        if outcome.kind == "done":
            dump_script(
                Script(goal=self.goal.goal, steps=self.script), self.log.dir / "script.yaml"
            )
        self.log.event("run.end", kind=outcome.kind, reason=outcome.reason, message=outcome.message)
        return outcome

    def _turn(self) -> None:
        stop = self.watch.before_call()
        if stop == "DEAD_END":
            self._escalate(
                "DEAD_END",
                f"the screen did not change across {self.config.limits.dead_end_repeats} actions",
            )
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
            self.log.event("agent.bad_call", turn=self.watch.steps, error=str(exc))
            self._reply(call_id, f"Rejected: {exc}", error=True)
            return

        self.log.event(
            "agent.decision",
            turn=self.watch.steps,
            tool=call.tool,
            input=call.model_dump(exclude={"tool", "reason"}),
            reason=call.reason,
            text=decision.text,
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
                "model.error", turn=self.watch.steps, error=f"{type(exc).__name__}: {exc}"
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
            self._escalate("STUCK", call.reason or "the agent could not continue")
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

        if not self._permitted(call_id, action, screen):
            return

        node = screen.find(call.ref) if call.ref else None
        try:
            result = self.surface.act(action)
        except SurfaceError as exc:
            self.log.event("action.failed", turn=self.watch.steps, error=str(exc))
            self._reply_with_screen(call_id, f"The action failed: {exc}", error=True)
            return

        self._record_step(call, node, screen)
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

    def _permitted(self, call_id: str, action: Action, screen: Observation) -> bool:
        decision = check(self.policy, action, screen)
        if isinstance(decision, Block):
            self.log.event("policy.block", turn=self.watch.steps, reason=decision.reason)
            self._reply(call_id, f"Blocked by policy: {decision.reason}", error=True)
            return False
        if isinstance(decision, NeedsApproval):
            if not self.config.auto_approve_risky:
                self._escalate("NEEDS_APPROVAL", decision.reason, rule=decision.rule)
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
        return True

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
        """Stop and ask for a human. The handoff itself lands with M6; until
        then the request is logged with everything it will carry, and the run
        ends."""
        screen = self.screen
        self.log.event(
            "escalation.requested",
            turn=self.watch.steps,
            reason_code=reason,
            message=message,
            location=screen.location if screen else None,
            frames=[f.model_dump() for f in screen.frames] if screen else [],
            note="human handoff is not wired up yet (M6); the run ends here",
            **fields,
        )
        raise _End("escalated", reason, message)

    # -- screens and messages ------------------------------------------------

    def _look(self) -> Observation:
        raw = self.surface.observe(
            screenshot=self.config.screenshots, masks=self.policy.screenshot_masks
        )
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
        self.script.append(
            ScriptStep(
                tool=call.tool,
                target=target,
                text=call.text if isinstance(call, TypeCall) else None,
                key=call.key if isinstance(call, PressCall) else None,
                reason=call.reason,
            )
        )
        self.log.event(
            "step",
            turn=self.watch.steps,
            tool=call.tool,
            node=node.model_dump(mode="json") if node else None,
            ladder=[r.model_dump(mode="json") for r in target] if target else None,
            text=call.text if isinstance(call, TypeCall) else None,
        )

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
