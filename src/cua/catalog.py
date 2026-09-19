"""The capabilities as a calling agent sees them: tools with typed arguments.

``cua catalog`` lists what is in ``capabilities/``; ``--json`` prints each
approved capability as a tool definition (``name``, ``description``,
``input_schema``) in the shape tool-calling model APIs take, so an agent can be
handed the catalog as its toolbox. ``cua catalog invoke`` is the other half:
call one by name, with the arguments a model would produce, and get a
``ReplayResult`` back.

Only approved capabilities are offered as tools. A draft is listed (with
``--all``) so a person can see it is there, but an agent is never shown
something that unattended replay would refuse to run.

Arguments are typed. A model's tool call carries JSON values, and the types
are checked here, before anything else: a ``decimal`` may come as a number or a
numeric string, an ``integer`` as an integer, but a ``string`` input must be a
JSON string. Member ids are strings on purpose — a number would lose a
leading zero, and quietly accepting one would teach the caller that it does
not matter. A wrong type is a ``Failure INPUT_INVALID`` with no browser
started, like every other malformed request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from cua.artifact.schema import Capability, InputSpec
from cua.artifact.store import ArtifactError, open_capability
from cua.replay.result import Failure


@dataclass(frozen=True)
class Entry:
    path: Path
    capability: Capability
    edited_outside: bool = False

    @property
    def invocable(self) -> bool:
        return self.capability.approval_state == "approved"


@dataclass(frozen=True)
class Unreadable:
    path: Path
    error: str


class CatalogError(LookupError):
    """No capability by that name."""


def scan(directory: Path) -> tuple[list[Entry], list[Unreadable]]:
    """Every capability file in ``directory`` (not recursive), by name."""
    entries: list[Entry] = []
    broken: list[Unreadable] = []
    for path in sorted(directory.glob("*.json")):
        try:
            loaded = open_capability(path)
        except ArtifactError as exc:
            broken.append(Unreadable(path, str(exc).splitlines()[0]))
            continue
        entries.append(Entry(path, loaded.capability, loaded.edited_outside))
    entries.sort(key=lambda e: e.capability.name)
    return entries, broken


def find(directory: Path, name: str) -> Path:
    """The file holding the capability called ``name``."""
    entries, _ = scan(directory)
    for entry in entries:
        if entry.capability.name == name:
            return entry.path
    known = ", ".join(e.capability.name for e in entries) or "none"
    raise CatalogError(f"no capability named {name!r} in {directory.as_posix()} (known: {known})")


# --------------------------------------------------------------------------
# Tool definitions
# --------------------------------------------------------------------------

_DECIMAL = r"^-?[0-9]+(\.[0-9]+)?$"


def tool_definition(cap: Capability) -> dict[str, Any]:
    properties = {name: _property(spec) for name, spec in cap.inputs.items()}
    return {
        "name": cap.name,
        "description": tool_description(cap),
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": [n for n, s in cap.inputs.items() if s.required],
            "additionalProperties": False,
        },
    }


def _property(spec: InputSpec) -> dict[str, Any]:
    out: dict[str, Any]
    if spec.type == "decimal":
        out = {"type": ["string", "number"], "pattern": _DECIMAL}
        hint = 'a decimal amount, e.g. "250.00"'
    elif spec.type == "integer":
        out = {"type": "integer"}
        hint = "an integer"
    else:
        out = {"type": "string"}
        if spec.pattern is not None:
            out["pattern"] = spec.pattern
        hint = "a string"
    description = spec.description or hint
    if spec.sensitive:
        description += " (sensitive: masked in every log)"
    out["description"] = description
    return out


def tool_description(cap: Capability) -> str:
    """What a model needs to decide to call it, and to read what comes back."""
    c = cap.contract
    parts = [cap.description.rstrip(".") + "."]
    if cap.outputs:
        outs = ", ".join(
            f"{n} ({o.type}{', may be absent' if o.optional else ''})"
            for n, o in cap.outputs.items()
        )
        parts.append(f"On success returns outputs: {outs}.")
    if c.side_effects == "none":
        parts.append("Read only: changes nothing.")
    else:
        parts.append(f"Side effect: {c.side_effects.replace('_', ' ')}.")
    if not c.idempotent:
        parts.append(
            "Not idempotent: pass an idempotency_key so that a retry after a lost answer "
            "returns the first result instead of doing it twice."
        )
    if c.may_escalate:
        parts.append(
            "Needs consent for its commit: without an approval token it returns kind "
            "'escalated' (reason NEEDS_APPROVAL) with a resume_token; once a person has "
            "consented, carry it on with `cua resume`."
        )
    if c.outcomes:
        parts.append(
            "May instead return kind 'business_outcome' with code one of: "
            + ", ".join(sorted(c.outcomes))
            + "; that is an answer about the member, not an error."
        )
    parts.append(
        "A 'failure' names the step, what was expected and seen, and side_effect "
        "(none, committed, or unknown: check before retrying)."
    )
    return " ".join(parts)


def tools(entries: list[Entry], *, include_drafts: bool = False) -> list[dict[str, Any]]:
    return [tool_definition(e.capability) for e in entries if e.invocable or include_drafts]


# --------------------------------------------------------------------------
# Typed arguments
# --------------------------------------------------------------------------


def coerce(cap: Capability, arguments: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """A tool call's JSON arguments as the string inputs replay takes, and
    every type problem found. Unknown names pass through for replay's own
    input check to name them."""
    out: dict[str, str] = {}
    problems: list[str] = []
    for name, value in arguments.items():
        spec = cap.inputs.get(name)
        text = _as_text(name, value, spec, problems)
        if text is not None:
            out[name] = text
    return out, problems


_A = {"string": "a string", "decimal": "a decimal", "integer": "an integer"}


def _as_text(name: str, value: Any, spec: InputSpec | None, problems: list[str]) -> str | None:
    if spec is None:
        return str(value)  # not an input at all: replay's input check says so
    shown = "***" if spec.sensitive else json.dumps(value, default=str)
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None or not isinstance(value, int | float | Decimal):
        problems.append(f"{name}={shown} is not {_A[spec.type]}")
        return None
    if spec.type == "decimal":
        exact = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        return format(exact, "f")
    if spec.type == "integer" and isinstance(value, int):
        return str(value)
    if spec.type == "string":
        problems.append(f"{name}={shown} must be a JSON string (quoted), not a number")
    else:
        problems.append(f"{name}={shown} is not {_A[spec.type]}")
    return None


def input_invalid(cap: Capability, problems: list[str], idempotency_key: str | None) -> Failure:
    return Failure(
        code="INPUT_INVALID",
        message="; ".join(problems),
        side_effect="none",
        capability=cap.name,
        capability_version=cap.version,
        idempotency_key=idempotency_key,
    )


# --------------------------------------------------------------------------
# The request envelope
# --------------------------------------------------------------------------

REQUEST_KEYS = frozenset(
    {"capability", "tenant", "inputs", "idempotency_key", "approval", "budget"}
)


def parse_request(text: str) -> dict[str, Any]:
    """An invocation request as JSON::

        {"capability": "open_subaccount", "tenant": "local",
         "inputs": {"member_id": "10003", "initial_deposit": 250.00},
         "idempotency_key": "agent-req-8f2a",
         "approval": {"token": "...", "approved_by": "reviewer"},
         "budget": {"timeout_s": 120, "max_recoveries": 3, "allow_escalation": true}}

    Numbers are read as exact decimals, never floats.
    """
    data = _object(text, "the request")
    unknown = sorted(set(data) - REQUEST_KEYS)
    if unknown:
        raise ValueError(f"unknown request field(s) {unknown}; allowed: {sorted(REQUEST_KEYS)}")
    if not isinstance(data.get("inputs", {}), dict):
        raise ValueError("inputs must be an object of name: value")
    return data


def parse_arguments(text: str) -> dict[str, Any]:
    """A tool call's arguments, ``{"member_id": "10003"}``."""
    return _object(text, "--args")


def _object(text: str, what: str) -> dict[str, Any]:
    try:
        data = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{what} is not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{what} must be a JSON object")
    return data
