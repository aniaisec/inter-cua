"""The model behind the loop, behind a protocol small enough to fake.

``AnthropicMessagesClient`` is the real one: one Messages API call per action.
``ScriptedClient`` plays back a script of tool calls, resolving each step's
locator ladder against the screen it is shown, so the loop, the policy check,
the stopping conditions and the evidence log all run for real with no key.

The loop keeps the conversation in the Messages API shape and hands it over
whole. It is append-only: an assistant turn is sent back exactly as the API
returned it (thinking blocks included), and earlier turns are never edited,
which is also what lets the conversation prefix stay cached turn to turn.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from cua.agent.script import Script, ScriptStep
from cua.surface.locators import Ladder, Resolved, resolve_ladder
from cua.surface.protocol import Observation

DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOKENS = 16_000


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    input: dict[str, Any]


class Decision(BaseModel):
    """One model turn, reduced to what the loop acts on and the log records."""

    model_config = ConfigDict(frozen=True)

    tool_call: ToolCall | None
    text: str = ""
    assistant_content: list[dict[str, Any]] = Field(default_factory=list)
    """The turn verbatim, to be appended to the conversation as sent."""
    response_id: str
    model: str
    stop_reason: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    system: str
    tools: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    observation: Observation
    """The screen the latest message describes. Only a scripted client reads
    it directly; a model reads the rendered copy in ``messages``."""


class LLMClient(Protocol):
    @property
    def model(self) -> str: ...

    def decide(self, request: DecisionRequest) -> Decision: ...


class AnthropicMessagesClient:
    def __init__(
        self,
        model: str | None = None,
        *,
        max_tokens: int = MAX_TOKENS,
        client: Any | None = None,
    ) -> None:
        import anthropic  # only discovery needs the SDK; replay must never import it

        self._model = model or os.environ.get("CUA_MODEL") or DEFAULT_MODEL
        self._max_tokens = max_tokens
        # Typed loosely on purpose: the loop builds messages as plain dicts in the
        # wire shape, which the SDK accepts but its TypedDicts cannot prove.
        self._client: Any = client or anthropic.Anthropic()

    @property
    def model(self) -> str:
        return self._model

    def decide(self, request: DecisionRequest) -> Decision:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=request.system,
            tools=request.tools,
            # At most one action per turn: the screen after one action is what
            # the next decision has to be made from.
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            messages=request.messages,
            # Caches the conversation up to its last block; with an
            # append-only history each turn reads the previous turn's prefix.
            cache_control={"type": "ephemeral"},
        )
        tool = next((b for b in response.content if b.type == "tool_use"), None)
        text = "".join(b.text for b in response.content if b.type == "text")
        usage = response.usage
        return Decision(
            tool_call=ToolCall(id=tool.id, name=tool.name, input=dict(tool.input))
            if tool is not None
            else None,
            text=text,
            assistant_content=[
                b.model_dump(mode="json", exclude_none=True) for b in response.content
            ],
            response_id=response.id,
            model=response.model,
            stop_reason=response.stop_reason,
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens or 0,
            },
        )


class ScriptedClient:
    """Plays a script back as if a model were choosing each step."""

    def __init__(self, script: Script) -> None:
        self._steps = list(script.steps)
        self._turn = 0

    @property
    def model(self) -> str:
        return "scripted"

    def decide(self, request: DecisionRequest) -> Decision:
        self._turn += 1
        if not self._steps:
            call = self._call("stuck", {"reason": "the script has no more steps"})
        else:
            call = self._resolve(self._steps.pop(0), request.observation)
        return Decision(
            tool_call=call,
            assistant_content=[
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}
            ],
            response_id=f"scripted_{self._turn:04d}",
            model=self.model,
            stop_reason="tool_use",
        )

    def _resolve(self, step: ScriptStep, observation: Observation) -> ToolCall:
        args: dict[str, Any] = {"reason": step.reason or f"scripted {step.tool}"}
        if step.target is not None:
            ref = _ref_or_none(step.target, observation)
            if ref is None:
                return self._call(
                    "stuck", {"reason": f"scripted {step.tool} target not on screen: {step.target}"}
                )
            args["ref"] = ref
        if step.text is not None:
            args["text"] = step.text
        if step.key is not None:
            args["key"] = step.key
        if step.tool == "done":
            outputs: dict[str, str] = {}
            for name, ladder in step.outputs.items():
                ref = _ref_or_none(ladder, observation)
                if ref is not None:
                    outputs[name] = ref
            args["outputs"] = outputs
        return self._call(step.tool, args)

    def _call(self, name: str, args: dict[str, Any]) -> ToolCall:
        return ToolCall(id=f"toolu_scripted_{self._turn:04d}", name=name, input=args)


def _ref_or_none(ladder: Ladder, observation: Observation) -> str | None:
    outcome = resolve_ladder(ladder, observation)
    return outcome.ref if isinstance(outcome, Resolved) else None
