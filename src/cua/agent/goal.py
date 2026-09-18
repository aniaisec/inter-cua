"""What a discovery run is asked to do, declared before it starts.

Parameters and outputs are declared up front rather than inferred from the
run afterwards. A recorder that guessed which typed literals were inputs would
turn a coincidence (a member id that happens to equal a branch number) into a
template; declaring ``member_id=10003`` first means only that value, typed
where the agent typed it, becomes ``${member_id}``.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ValueType = Literal["string", "decimal", "integer"]
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class ParamSpec(BaseModel):
    """An input the calling agent will supply, with the value used for discovery."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: ValueType = "string"
    value: str


class OutputSpec(BaseModel):
    """An output the calling agent wants back."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: ValueType = "string"
    optional: bool = False
    description: str = ""


class Goal(BaseModel):
    model_config = ConfigDict(frozen=True)

    goal: str
    name: str
    """Capability name the run will be recorded under."""
    entry: str = "/"
    """Where the run starts, relative to the tenant's base URL."""
    params: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    credentials: dict[str, str] = Field(default_factory=dict)
    """Credential name → ``secret://`` ref. Values never enter the goal."""


class SpecError(ValueError):
    pass


def parse_param(text: str) -> ParamSpec:
    """``member_id:string=10003`` (the type defaults to string)."""
    head, sep, value = text.partition("=")
    if not sep:
        raise SpecError(f"--param wants name[:type]=value, got {text!r}")
    name, _, kind = head.partition(":")
    return ParamSpec(name=_name(name), type=_type(kind or "string"), value=value)


def parse_output(text: str) -> OutputSpec:
    """``savings_balance:decimal``; a trailing ``?`` makes it optional; an
    optional ``=description`` says what it is."""
    head, _, description = text.partition("=")
    optional = head.endswith("?")
    name, _, kind = head.rstrip("?").partition(":")
    return OutputSpec(
        name=_name(name),
        type=_type(kind or "string"),
        optional=optional,
        description=description,
    )


def parse_credential(text: str) -> tuple[str, str]:
    """``app_login=secret://local/mockcore/operator``."""
    name, sep, ref = text.partition("=")
    if not sep:
        raise SpecError(f"--credential wants name=secret://..., got {text!r}")
    return _name(name), ref


def _name(name: str) -> str:
    if not _NAME.match(name):
        raise SpecError(f"{name!r} is not a valid name (lower_snake_case)")
    return name


def _type(kind: str) -> ValueType:
    if kind == "string":
        return "string"
    if kind == "decimal":
        return "decimal"
    if kind == "integer":
        return "integer"
    raise SpecError(f"unknown type {kind!r}; use string, decimal or integer")
