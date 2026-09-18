"""The six things the discovery agent can do.

``click``, ``type``, ``press`` and ``read`` act on refs from the latest
observation; ``done`` ends the run by naming, for every declared output, the
ref it was read from; ``stuck`` ends it by asking for a human. There is no
``navigate``: the agent reaches screens the way an operator does, through the
app, so every step it takes is a step a recorded capability can replay.

Every tool takes a ``reason``. It is what the evidence log records as *why*
the agent did what it did, and it costs the model one sentence.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cua.agent.goal import OutputSpec
from cua.agent.llm import ToolSpec


class _Call(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    reason: str = ""


class ClickCall(_Call):
    tool: Literal["click"] = "click"
    ref: str


class TypeCall(_Call):
    tool: Literal["type"] = "type"
    ref: str
    text: str


class PressCall(_Call):
    tool: Literal["press"] = "press"
    key: str
    ref: str | None = None


class ReadCall(_Call):
    tool: Literal["read"] = "read"
    ref: str


class DoneCall(_Call):
    tool: Literal["done"] = "done"
    outputs: dict[str, str] = Field(default_factory=dict)
    """Output name → the ref holding its value."""


class StuckCall(_Call):
    tool: Literal["stuck"] = "stuck"


AgentCall: TypeAlias = Annotated[
    ClickCall | TypeCall | PressCall | ReadCall | DoneCall | StuckCall,
    Field(discriminator="tool"),
]


class _Envelope(BaseModel):
    call: AgentCall


class ToolInputError(ValueError):
    """The model called a tool that does not exist, or with the wrong shape."""


def parse_call(name: str, arguments: dict[str, Any]) -> AgentCall:
    try:
        return _Envelope.model_validate({"call": {**arguments, "tool": name}}).call
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'][1:]) or name}: {e['msg']}" for e in exc.errors()
        )
        raise ToolInputError(f"{name}: {problems}") from exc


_REASON = {
    "type": "string",
    "description": "One sentence: why this, now. Recorded in the evidence log.",
}
_REF = {"type": "string", "description": "A ref from the latest screen, e.g. n42."}


def tool_definitions(outputs: list[OutputSpec]) -> list[ToolSpec]:
    """The tools as JSON Schema, for whichever provider is asked.

    ``done`` is built per run, with one property per declared output, so the
    model is shown exactly what it has to hand back.
    """
    output_props = {
        o.name: {
            "type": "string",
            "description": f"Ref of the node holding {o.name} ({o.type})"
            + (f": {o.description}" if o.description else ""),
        }
        for o in outputs
    }
    specs: list[dict[str, Any]] = [
        {
            "name": "click",
            "description": "Click a link, button or other control on the current screen.",
            "parameters": {
                "type": "object",
                "properties": {"ref": _REF, "reason": _REASON},
                "required": ["ref", "reason"],
            },
        },
        {
            "name": "type",
            "description": (
                "Replace the contents of a text field. For a credential, type its placeholder "
                "(e.g. ${credentials.app_login.password}); the runner substitutes the value."
            ),
            "parameters": {
                "type": "object",
                "properties": {"ref": _REF, "text": {"type": "string"}, "reason": _REASON},
                "required": ["ref", "text", "reason"],
            },
        },
        {
            "name": "press",
            "description": "Press a key (e.g. Enter, Tab), in a field if ref is given.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "ref": _REF, "reason": _REASON},
                "required": ["key", "reason"],
            },
        },
        {
            "name": "read",
            "description": "Read the current text of a node, live from the screen.",
            "parameters": {
                "type": "object",
                "properties": {"ref": _REF, "reason": _REASON},
                "required": ["ref", "reason"],
            },
        },
        {
            "name": "done",
            "description": (
                "The goal is reached. Name, for every declared output, the ref on the current "
                "screen that holds its value. The runner reads each one itself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "outputs": {
                        "type": "object",
                        "properties": output_props,
                        "required": [o.name for o in outputs if not o.optional],
                    },
                    "reason": _REASON,
                },
                "required": ["outputs", "reason"],
            },
        },
        {
            "name": "stuck",
            "description": (
                "You cannot make progress without guessing. Say what blocks you; a human "
                "takes over. Always better than inventing an id, a value or a screen."
            ),
            "parameters": {
                "type": "object",
                "properties": {"reason": _REASON},
                "required": ["reason"],
            },
        },
    ]
    return [ToolSpec.model_validate(spec) for spec in specs]
