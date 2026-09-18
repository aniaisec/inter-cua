"""The model behind the loop, behind a protocol small enough to fake.

The loop keeps its conversation as a provider-neutral transcript — what it
told the model, what the model decided, what each tool call returned — and
each client translates that into its own API's shape:

``AnthropicMessagesClient``  Claude, over the Messages API
``GeminiClient``             Gemini, over the Gen AI SDK
``ScriptedClient``           no model at all: plays back a script of tool
                             calls, so the whole loop runs with no key

``select_client`` picks one from the keys that are present, so nothing above
this module knows or cares which provider answered.

The transcript is append-only, and a model's own turn is sent back exactly as
its API returned it (``Decision.raw``). Both providers need that: Claude's
thinking blocks and Gemini's thought signatures are only valid if returned
unchanged, and an unchanged prefix is also what keeps the conversation cached
from one turn to the next.
"""

from __future__ import annotations

import os
from typing import Any, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from cua.agent.script import Script, ScriptStep
from cua.surface.locators import Ladder, Resolved, resolve_ladder
from cua.surface.protocol import Observation

ANTHROPIC_MODEL = "claude-sonnet-5"
GEMINI_MODEL = "gemini-flash-latest"
"""An alias Google keeps pointed at its current Flash model, so the default
does not go stale. Evidence records the exact version that answered
(``model_version``), so pinning is only needed for reproducibility."""
MAX_TOKENS = 16_000
GEMINI_ATTEMPTS = 5

Provider = Literal["anthropic", "gemini", "scripted"]


# --------------------------------------------------------------------------
# The neutral transcript
# --------------------------------------------------------------------------


class ToolSpec(BaseModel):
    """A tool the model may call: a name, what it is for, and a JSON Schema."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    parameters: dict[str, Any]


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    input: dict[str, Any]


class UserTurn(BaseModel):
    """Something the loop tells the model outside of a tool result."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["user"] = "user"
    text: str
    png: bytes | None = Field(default=None, repr=False)


class ToolResultTurn(BaseModel):
    """What one tool call did, and the screen it left behind."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["tool_result"] = "tool_result"
    call_id: str
    tool: str
    text: str
    png: bytes | None = Field(default=None, repr=False)
    is_error: bool = False


class Decision(BaseModel):
    """One model turn, reduced to what the loop acts on and the log records."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    kind: Literal["decision"] = "decision"
    tool_call: ToolCall | None
    text: str = ""
    raw: Any = Field(default=None, exclude=True, repr=False)
    """The turn exactly as the provider returned it, to be sent back as is.
    Opaque to everything but the client that produced it."""
    response_id: str
    model: str
    stop_reason: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)


Turn: TypeAlias = UserTurn | ToolResultTurn | Decision


class DecisionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    system: str
    tools: list[ToolSpec]
    transcript: list[Turn]
    observation: Observation
    """The screen the latest turn describes. Only a scripted client reads it
    directly; a model reads the rendered copy in ``transcript``."""


class LLMClient(Protocol):
    @property
    def provider(self) -> Provider: ...

    @property
    def model(self) -> str: ...

    def decide(self, request: DecisionRequest) -> Decision: ...


class NoProviderError(RuntimeError):
    """No key for any provider, and no script to play instead."""


def select_client(choice: str = "auto", model: str | None = None) -> LLMClient:
    """The client for ``choice``, or for whichever key is present.

    ``auto`` prefers Claude when both keys are set; set ``CUA_LLM`` (or pass
    ``--llm``) to choose. The model comes from ``--model``, else the
    provider's own variable (``CUA_MODEL`` for Claude, ``CUA_GEMINI_MODEL``
    for Gemini), else the default above — never one provider's model name
    sent to the other.
    """
    if choice == "auto":
        choice = os.environ.get("CUA_LLM") or "auto"
    if choice == "auto":
        choice = next((p for p, keys in PROVIDER_KEYS.items() if _has_key(keys)), "auto")
        if choice == "auto":
            raise NoProviderError(
                "no model API key found: set ANTHROPIC_API_KEY or GEMINI_API_KEY in .env, "
                "or run with --llm scripted --script <file>"
            )
    if choice not in PROVIDER_KEYS:
        raise NoProviderError(f"unknown model provider {choice!r}; use anthropic, gemini or auto")
    # Checked here, before a browser is launched: without it the run starts,
    # and fails only at the first model call, as an opaque SDK error.
    if not _has_key(PROVIDER_KEYS[choice]):
        raise NoProviderError(
            f"--llm {choice} needs {' or '.join(PROVIDER_KEYS[choice])} set in .env"
        )
    if choice == "anthropic":
        return AnthropicMessagesClient(model or os.environ.get("CUA_MODEL") or ANTHROPIC_MODEL)
    return GeminiClient(model or os.environ.get("CUA_GEMINI_MODEL") or GEMINI_MODEL)


PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}
"""The variables each SDK reads its credential from. Order is preference for
``auto``: Claude first, as planned."""


def _has_key(names: tuple[str, ...]) -> bool:
    return any(os.environ.get(n) for n in names)


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


class AnthropicMessagesClient:
    def __init__(
        self,
        model: str = ANTHROPIC_MODEL,
        *,
        max_tokens: int = MAX_TOKENS,
        client: Any | None = None,
    ) -> None:
        import anthropic  # only discovery needs a model SDK; replay must never import one

        self._model = model
        self._max_tokens = max_tokens
        # Typed loosely on purpose: messages are built as plain dicts in the
        # wire shape, which the SDK accepts but its TypedDicts cannot prove.
        self._client: Any = client or anthropic.Anthropic()

    @property
    def provider(self) -> Provider:
        return "anthropic"

    @property
    def model(self) -> str:
        return self._model

    def decide(self, request: DecisionRequest) -> Decision:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=request.system,
            tools=[
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in request.tools
            ],
            # At most one action per turn: the screen after one action is what
            # the next decision has to be made from.
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            messages=_anthropic_messages(request.transcript),
            # Caches the conversation up to its last block; with an
            # append-only transcript each turn reads the previous turn's prefix.
            cache_control={"type": "ephemeral"},
        )
        tool = next((b for b in response.content if b.type == "tool_use"), None)
        usage = response.usage
        return Decision(
            tool_call=ToolCall(id=tool.id, name=tool.name, input=dict(tool.input))
            if tool is not None
            else None,
            text="".join(b.text for b in response.content if b.type == "text"),
            raw=[b.model_dump(mode="json", exclude_none=True) for b in response.content],
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


def _anthropic_messages(transcript: list[Turn]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for turn in transcript:
        if isinstance(turn, UserTurn):
            messages.append({"role": "user", "content": _anthropic_blocks(turn.text, turn.png)})
        elif isinstance(turn, ToolResultTurn):
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": turn.call_id,
                "content": _anthropic_blocks(turn.text, turn.png),
            }
            if turn.is_error:
                block["is_error"] = True
            messages.append({"role": "user", "content": [block]})
        else:
            messages.append({"role": "assistant", "content": _anthropic_assistant(turn)})
    return messages


def _anthropic_assistant(decision: Decision) -> list[dict[str, Any]]:
    if isinstance(decision.raw, list) and decision.raw:
        return decision.raw
    blocks: list[dict[str, Any]] = []
    if decision.text:
        blocks.append({"type": "text", "text": decision.text})
    if decision.tool_call is not None:
        call = decision.tool_call
        blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.input})
    return blocks or [{"type": "text", "text": "(no output)"}]


def _anthropic_blocks(text: str, png: bytes | None) -> list[dict[str, Any]]:
    import base64

    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if png:
        data = base64.standard_b64encode(png).decode("ascii")
        blocks.append(
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
        )
    return blocks


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------


class GeminiClient:
    def __init__(
        self,
        model: str = GEMINI_MODEL,
        *,
        max_tokens: int = MAX_TOKENS,
        client: Any | None = None,
    ) -> None:
        from google import genai  # only discovery needs a model SDK
        from google.genai import types

        self._model = model
        self._max_tokens = max_tokens
        # Reads GEMINI_API_KEY / GOOGLE_API_KEY. Unlike the Anthropic SDK, this
        # one does not retry by default, and a busy model answers 503 — so
        # overload and rate limits are retried with backoff, as they are for
        # Claude. Anything else fails the call at once.
        self._client: Any = client or genai.Client(
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(
                    attempts=GEMINI_ATTEMPTS,
                    initial_delay=2.0,
                    max_delay=30.0,
                    http_status_codes=[408, 429, 500, 502, 503, 504],
                )
            )
        )
        self._calls = 0

    @property
    def provider(self) -> Provider:
        return "gemini"

    @property
    def model(self) -> str:
        return self._model

    def decide(self, request: DecisionRequest) -> Decision:
        from google.genai import types

        self._calls += 1
        config = types.GenerateContentConfig(
            system_instruction=request.system,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            parameters_json_schema=t.parameters,
                        )
                        for t in request.tools
                    ]
                )
            ],
            # ANY: every turn must be a function call. The loop runs the
            # tools itself, so the SDK must not try to.
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.ANY
                )
            ),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=self._max_tokens,
        )
        response = self._client.models.generate_content(
            model=self._model, contents=_gemini_contents(request.transcript), config=config
        )

        candidate = response.candidates[0] if response.candidates else None
        content = candidate.content if candidate is not None else None
        parts = list(content.parts or []) if content is not None else []
        first_call = next((i for i, p in enumerate(parts) if p.function_call), None)
        tool_call: ToolCall | None = None
        if first_call is not None:
            # One action per turn. A model that asked for several has the
            # rest dropped from its turn too, so the transcript never holds a
            # call that was not answered.
            parts = parts[: first_call + 1]
            call = parts[first_call].function_call
            tool_call = ToolCall(
                id=call.id or f"gemini_call_{self._calls:04d}",
                name=call.name or "",
                input=dict(call.args or {}),
            )
        usage = response.usage_metadata
        return Decision(
            tool_call=tool_call,
            text="".join(p.text for p in parts if p.text and not p.thought),
            raw=types.Content(role="model", parts=parts) if parts else None,
            response_id=response.response_id or f"gemini_{self._calls:04d}",
            model=response.model_version or self._model,
            stop_reason=str(candidate.finish_reason.value)
            if candidate is not None and candidate.finish_reason is not None
            else None,
            usage={
                "input_tokens": (usage.prompt_token_count or 0) if usage else 0,
                "output_tokens": (usage.candidates_token_count or 0) if usage else 0,
                "thinking_tokens": (usage.thoughts_token_count or 0) if usage else 0,
                "cache_read_input_tokens": (usage.cached_content_token_count or 0) if usage else 0,
            },
        )


def _gemini_contents(transcript: list[Turn]) -> list[Any]:
    from google.genai import types

    def media(text: str, png: bytes | None) -> list[types.Part]:
        parts = [types.Part.from_text(text=text)]
        if png:
            parts.append(types.Part.from_bytes(data=png, mime_type="image/png"))
        return parts

    contents: list[types.Content] = []
    for turn in transcript:
        if isinstance(turn, UserTurn):
            contents.append(types.Content(role="user", parts=media(turn.text, turn.png)))
        elif isinstance(turn, ToolResultTurn):
            answer = types.Part(
                function_response=types.FunctionResponse(
                    # Ids the API did not assign are ours, and mean nothing to it.
                    id=None if turn.call_id.startswith("gemini_call_") else turn.call_id,
                    name=turn.tool,
                    response={"error" if turn.is_error else "output": turn.text},
                )
            )
            # The screen rides along as an ordinary image part after the
            # function response it belongs to.
            extra = (
                [types.Part.from_bytes(data=turn.png, mime_type="image/png")] if turn.png else []
            )
            contents.append(types.Content(role="user", parts=[answer, *extra]))
        elif isinstance(turn.raw, types.Content):
            contents.append(turn.raw)
        else:
            parts: list[types.Part] = []
            if turn.text:
                parts.append(types.Part.from_text(text=turn.text))
            if turn.tool_call is not None:
                parts.append(
                    types.Part(
                        function_call=types.FunctionCall(
                            name=turn.tool_call.name, args=turn.tool_call.input
                        )
                    )
                )
            if not parts:
                parts.append(types.Part.from_text(text="(no output)"))
            contents.append(types.Content(role="model", parts=parts))
    return contents


# --------------------------------------------------------------------------
# Scripted
# --------------------------------------------------------------------------


class ScriptedClient:
    """Plays a script back as if a model were choosing each step."""

    def __init__(self, script: Script) -> None:
        self._steps = list(script.steps)
        self._turn = 0

    @property
    def provider(self) -> Provider:
        return "scripted"

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
