"""The invocation request, and everything checked about it before a browser starts.

What a calling agent (or ``cua replay``) sends::

    capability: member_savings_balance
    tenant: local
    inputs: {member_id: "10003"}
    idempotency_key: agent-req-8f2a
    approval: {token: "...", approved_by: "..."}
    budget: {timeout_s: 120, max_recoveries: 3, allow_escalation: true}

Input shape is validated against the capability's declared ``inputs`` here, so a
malformed request fails as ``INPUT_INVALID`` with nothing touched — the cheapest
failure there is, and the only one that is certainly free of side effects.
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.schema import PLACEHOLDER, Capability, InputSpec

_CREDENTIAL = re.compile(r"^credentials\.([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)$")


class Budget(BaseModel):
    """How much a caller is willing to spend on one invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_s: float = Field(default=120.0, gt=0)
    """Wall clock for the whole run, recoveries included."""
    max_recoveries: int = Field(default=3, ge=0)
    """Caps the capability's own ``recovery_limits.per_run``; never raises it."""
    allow_escalation: bool = True
    """False: never wait on a human. A fault that would have been handed to
    one comes back as a ``Failure`` naming the escalation it replaced."""


class ApprovalGrant(BaseModel):
    """Consent the caller says it holds for the capability's risky steps.

    Nothing here is trusted as it stands: the runner verifies the token
    (``cua.policy.tokens``) before a browser starts, and only the consent it
    proves satisfies ``approval: required`` and the policy's risky rules, for
    this invocation only. The token is logged by hash, never in the clear.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: str = Field(min_length=1)
    approved_by: str | None = None
    """If given, must be who the token names."""

    @property
    def token_sha256(self) -> str:
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()


class Invocation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    inputs: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str | None = None
    approval: ApprovalGrant | None = None
    budget: Budget = Field(default_factory=Budget)
    inject: str | None = None
    """Demo only: arms a mock-app failure mode on the first request."""


def validate_inputs(capability: Capability, inputs: dict[str, str]) -> list[str]:
    """Every problem with the inputs, or none. Checked before the browser starts."""
    problems: list[str] = []
    for name in inputs:
        if name not in capability.inputs:
            declared = ", ".join(sorted(capability.inputs)) or "none"
            problems.append(f"{name!r} is not an input of {capability.name} (declared: {declared})")
    for name, spec in capability.inputs.items():
        if name not in inputs:
            if spec.required:
                problems.append(f"{name!r} is required")
            continue
        problem = check_input(name, inputs[name], spec)
        if problem:
            problems.append(problem)
    return problems


def check_input(name: str, value: str, spec: InputSpec) -> str | None:
    """One value against its declared type and pattern; the problem, or None."""
    shown = "***" if spec.sensitive else repr(value)
    if spec.type == "decimal":
        try:
            Decimal(value)
        except InvalidOperation:
            return f"{name}={shown} is not a decimal"
    elif spec.type == "integer" and not re.fullmatch(r"-?[0-9]+", value):
        return f"{name}={shown} is not an integer"
    if spec.pattern is not None and re.search(spec.pattern, value) is None:
        return f"{name}={shown} does not match {spec.pattern}"
    return None


def fill(text: str, inputs: dict[str, str], *, regex: bool = False) -> str:
    """Substitute ``${input}`` placeholders. Credential placeholders are left
    alone: they are resolved only at the moment of typing, by ``expand``.

    ``regex``: the text is a pattern, so the value is escaped — a member id
    must match itself, not be read as a pattern of its own.
    """

    def one(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in inputs:
            return match.group(0)
        return re.escape(inputs[name]) if regex else inputs[name]

    return PLACEHOLDER.sub(one, text)


C = TypeVar("C", bound=BaseModel)


def bind(condition: C, inputs: dict[str, str]) -> C:
    """A condition with this invocation's input values filled in."""
    data = _bind_data(condition.model_dump(mode="json"), inputs, key="")
    return type(condition).model_validate(data)


def _bind_data(data: Any, inputs: dict[str, str], *, key: str) -> Any:
    if isinstance(data, str):
        return fill(data, inputs, regex=key == "pattern")
    if isinstance(data, dict):
        return {k: _bind_data(v, inputs, key=k) for k, v in data.items()}
    if isinstance(data, list):
        return [_bind_data(v, inputs, key=key) for v in data]
    return data


def input_names(text: str) -> list[str]:
    """The ``${input}`` placeholders in a text, credentials excluded."""
    return [m.group(1) for m in PLACEHOLDER.finditer(text) if _CREDENTIAL.match(m.group(1)) is None]


def credential_field(placeholder: str) -> tuple[str, str] | None:
    found = _CREDENTIAL.match(placeholder)
    return (found.group(1), found.group(2)) if found else None


def request_summary(invocation: Invocation, capability: Capability) -> dict[str, Any]:
    """The invocation as it may be logged: sensitive inputs masked, the
    approval token reduced to a hash prefix."""
    inputs = {
        name: ("***" if capability.inputs.get(name, InputSpec()).sensitive else value)
        for name, value in invocation.inputs.items()
    }
    approval = (
        {
            "approved_by": invocation.approval.approved_by,
            "token_sha256": invocation.approval.token_sha256[:12],
        }
        if invocation.approval
        else None
    )
    return {
        "inputs": inputs,
        "idempotency_key": invocation.idempotency_key,
        "approval": approval,
        "budget": invocation.budget.model_dump(),
        "inject": invocation.inject,
    }
