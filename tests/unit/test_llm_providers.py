"""Each provider's client, translating the neutral transcript both ways.

The SDK clients are faked at the one call each makes, so these run with no
key and no network. What they pin down is the part that breaks silently when
wrong: a model's own turn going back verbatim (Claude's thinking blocks,
Gemini's thought signatures), every tool call answered by id, and the screen
image travelling with the tool result it belongs to.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import types

from cua.agent.goal import OutputSpec
from cua.agent.llm import (
    AnthropicMessagesClient,
    DecisionRequest,
    GeminiClient,
    NoProviderError,
    ToolResultTurn,
    Turn,
    UserTurn,
    select_client,
)
from cua.agent.tools import tool_definitions
from tests.unit import screens

PNG = b"\x89PNG screen"
TOOLS = tool_definitions([OutputSpec(name="savings_balance", type="decimal")])
SCREEN = screens.build({"": screens.LOGIN})


def request(transcript: list[Turn]) -> DecisionRequest:
    return DecisionRequest(system="sys", tools=TOOLS, transcript=transcript, observation=SCREEN)


class Capture:
    """Stands in for an SDK method: records its kwargs, returns a canned reply."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.replies.pop(0)


# -- Anthropic -----------------------------------------------------------------


def anthropic_reply(*blocks: dict[str, Any]) -> Any:
    def block(b: dict[str, Any]) -> Any:
        return SimpleNamespace(**b, model_dump=lambda **_: dict(b))

    return SimpleNamespace(
        id="msg_1",
        model="claude-sonnet-5",
        stop_reason="tool_use",
        content=[block(b) for b in blocks],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def test_claude_gets_its_turn_back_verbatim_and_the_screen_with_the_tool_result() -> None:
    thinking = {"type": "thinking", "thinking": "", "signature": "sig-abc"}
    tool_use = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "click",
        "input": {"ref": "n9", "reason": "sign on"},
    }
    create = Capture(anthropic_reply(thinking, tool_use), anthropic_reply(tool_use))
    client = AnthropicMessagesClient(
        client=SimpleNamespace(messages=SimpleNamespace(create=create))
    )

    first = client.decide(request([UserTurn(text="first screen", png=PNG)]))
    assert first.tool_call is not None and first.tool_call.id == "toolu_1"
    assert first.usage["input_tokens"] == 10

    client.decide(
        request(
            [
                UserTurn(text="first screen", png=PNG),
                first,
                ToolResultTurn(call_id="toolu_1", tool="click", text="done", png=PNG),
            ]
        )
    )
    sent = create.calls[1]
    assert sent["tools"][0]["input_schema"]["required"] == ["ref", "reason"]
    user, assistant, result = sent["messages"]
    assert [b["type"] for b in user["content"]] == ["text", "image"]
    assert assistant == {"role": "assistant", "content": [thinking, tool_use]}
    (block,) = result["content"]
    assert block["tool_use_id"] == "toolu_1"
    assert [b["type"] for b in block["content"]] == ["text", "image"]
    assert sent["tool_choice"]["disable_parallel_tool_use"] is True


# -- Gemini --------------------------------------------------------------------


def gemini_reply(*parts: types.Part) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=list(parts)),
                finish_reason=types.FinishReason.STOP,
            )
        ],
        response_id="resp_1",
        model_version="gemini-flash-test-001",
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=12, candidates_token_count=4, thoughts_token_count=7
        ),
    )


def call_part(call_id: str | None, name: str, args: dict[str, Any], sig: bytes | None = None):
    return types.Part(
        function_call=types.FunctionCall(id=call_id, name=name, args=args),
        thought_signature=sig,
    )


def gemini(*replies: types.GenerateContentResponse) -> tuple[GeminiClient, Capture]:
    generate = Capture(*replies)
    fake = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    return GeminiClient("gemini-flash-latest", client=fake), generate


def test_gemini_gets_its_signed_turn_back_verbatim_and_answers_the_call_by_id() -> None:
    signed = call_part("call_1", "click", {"ref": "n9", "reason": "sign on"}, sig=b"sig-xyz")
    client, generate = gemini(gemini_reply(signed), gemini_reply(signed))

    first = client.decide(request([UserTurn(text="first screen", png=PNG)]))
    assert first.tool_call is not None
    assert (first.tool_call.id, first.tool_call.name) == ("call_1", "click")
    assert first.model == "gemini-flash-test-001"
    assert first.usage == {
        "input_tokens": 12,
        "output_tokens": 4,
        "thinking_tokens": 7,
        "cache_read_input_tokens": 0,
    }

    client.decide(
        request(
            [
                UserTurn(text="first screen", png=PNG),
                first,
                ToolResultTurn(call_id="call_1", tool="click", text="clicked", png=PNG),
            ]
        )
    )
    sent = generate.calls[1]
    user, model_turn, result = sent["contents"]
    assert user.parts[1].inline_data.mime_type == "image/png"
    assert model_turn.parts[0].thought_signature == b"sig-xyz"
    answer, image = result.parts
    assert answer.function_response.id == "call_1"
    assert answer.function_response.name == "click"
    assert answer.function_response.response == {"output": "clicked"}
    assert image.inline_data.data == PNG

    config = sent["config"]
    assert config.tool_config.function_calling_config.mode == types.FunctionCallingConfigMode.ANY
    assert config.automatic_function_calling.disable is True
    declared = {d.name: d for d in config.tools[0].function_declarations}
    assert declared["done"].parameters_json_schema["properties"]["outputs"]["required"] == [
        "savings_balance"
    ]


def test_gemini_asking_for_two_actions_gets_one_and_its_turn_holds_only_that_one() -> None:
    client, _ = gemini(
        gemini_reply(
            call_part(None, "type", {"ref": "n8", "text": "x", "reason": "a"}),
            call_part(None, "click", {"ref": "n9", "reason": "b"}),
        )
    )
    decision = client.decide(request([UserTurn(text="screen")]))

    assert decision.tool_call is not None and decision.tool_call.name == "type"
    assert len(decision.raw.parts) == 1


def test_an_error_result_is_reported_to_gemini_as_an_error() -> None:
    reply = gemini_reply(call_part(None, "stuck", {"reason": "r"}))
    client, generate = gemini(reply, reply)
    first = client.decide(request([UserTurn(text="screen")]))
    assert first.tool_call is not None
    client.decide(
        request(
            [
                UserTurn(text="screen"),
                first,
                ToolResultTurn(
                    call_id=first.tool_call.id, tool="stuck", text="no such ref", is_error=True
                ),
            ]
        )
    )
    answer = generate.calls[-1]["contents"][-1].parts[0].function_response
    assert answer.response == {"error": "no such ref"}
    # An id the API never assigned is not sent back to it.
    assert answer.id is None


# -- choosing a provider -------------------------------------------------------


@pytest.fixture
def no_keys(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for var in (
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "CUA_LLM",
        "CUA_MODEL",
        "CUA_GEMINI_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_auto_uses_whichever_key_is_present(no_keys: pytest.MonkeyPatch) -> None:
    no_keys.setenv("GEMINI_API_KEY", "test-not-a-key")
    client = select_client("auto")
    assert (client.provider, client.model) == ("gemini", "gemini-flash-latest")

    no_keys.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    assert select_client("auto").provider == "anthropic"


def test_cua_llm_overrides_the_preference_when_both_keys_are_set(
    no_keys: pytest.MonkeyPatch,
) -> None:
    no_keys.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    no_keys.setenv("GEMINI_API_KEY", "test-not-a-key")
    no_keys.setenv("CUA_LLM", "gemini")
    assert select_client("auto").provider == "gemini"


def test_each_provider_reads_its_own_model_variable(no_keys: pytest.MonkeyPatch) -> None:
    no_keys.setenv("GEMINI_API_KEY", "test-not-a-key")
    no_keys.setenv("CUA_MODEL", "claude-sonnet-5")
    no_keys.setenv("CUA_GEMINI_MODEL", "gemini-pro-latest")
    # A Claude model name must never reach Gemini.
    assert select_client("gemini").model == "gemini-pro-latest"
    assert select_client("gemini", "gemini-x").model == "gemini-x"


def test_no_key_at_all_says_what_to_do(no_keys: pytest.MonkeyPatch) -> None:
    with pytest.raises(NoProviderError, match="GEMINI_API_KEY"):
        select_client("auto")
