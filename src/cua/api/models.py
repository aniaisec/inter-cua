"""Request and response bodies of the HTTP API, and its one error type.

A run is answered with an ``ApiRun``: the API's record of the invocation
(who asked, for which tenant, under which request id and idempotency key, and
where it stands) with the ``ReplayResult`` inside it, exactly as ``cua
replay`` would print it once the run has one. The result is never reshaped:
a caller branches on ``result.kind`` over HTTP as it would on the CLI's JSON.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.replay.invocation import ApprovalGrant, Budget

RunState = Literal["running", "escalated", "finished", "error", "lost"]
"""``running``: a worker holds it (a first run or a resumed one).
``escalated``: it waits on a person or on consent; ``approve``, ``resume`` and
``abort`` apply. ``finished``: it has a result that is an answer (success,
business outcome or failure). ``error``: the request could not be carried
out at all, as ``cua replay`` exits 64 (a credential that does not resolve,
say); there is no result. ``lost``: this server was stopped while the run was
under way and its run directory holds no result; what it did is in its
evidence, and its side effect must be found out, not assumed."""


class ApiError(Exception):
    """An answer that is not a run: the request itself was refused."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HandoffOptions(_Body):
    """Hand a stuck run to a person (``cua replay --handoff``): the browser is
    left up and the run comes back ``escalated`` at once; the operator console
    (``cua operator``) shows the request."""

    ttl_s: float = Field(default=1800.0, gt=0, le=24 * 60 * 60)
    """How long the request stays open before it is aborted."""


class RunRequest(_Body):
    """``POST /runs``. The idempotency key travels in the ``Idempotency-Key``
    header, not here."""

    capability: str = Field(min_length=1)
    version: int | None = Field(default=None, ge=1)
    """Pin a version; by default the one a call by name runs."""
    inputs: dict[str, Any] = Field(default_factory=dict)
    """Typed JSON values, checked as ``cua catalog invoke --args`` checks them."""
    approval: ApprovalGrant | None = None
    """Consent for the capability's risky steps (``cua approval-token``)."""
    budget: Budget = Field(default_factory=Budget)
    handoff: HandoffOptions | None = None
    screenshots: bool = True
    vision: bool = False
    inject: str | None = None
    """Demo only: arm a mock-app failure mode. Refused unless the server was
    started with ``--allow-inject``."""


class ApproveRequest(_Body):
    """``POST /runs/{id}/approve``: consent for the step an escalated run
    stopped at (``NEEDS_APPROVAL``). A signed token, never a name alone: over
    HTTP there is no person at a console to vouch for who is consenting."""

    token: str = Field(min_length=1)
    approved_by: str | None = None
    inputs: dict[str, str] = Field(default_factory=dict)
    """Sensitive inputs, supplied again: a run keeps only their hashes."""


class ResumeRequest(_Body):
    """``POST /runs/{id}/resume``: hand an escalated run back to the
    automation (after a person has worked on it, or to have it look again)."""

    resume_at: str | None = None
    inputs: dict[str, str] = Field(default_factory=dict)


class AbortRequest(_Body):
    why: str = ""


class ApiRun(BaseModel):
    """The API's record of one invocation."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    """Also the run directory's name, when the run got as far as having one."""
    state: RunState
    capability: str
    version: int | None = None
    """The version the run executes; None until it is known."""
    tenant: str
    client: str
    request_id: str
    """The ``X-Request-Id`` of the request that started it."""
    idempotency_key: str | None = None
    created_at: str
    updated_at: str
    result: dict[str, Any] | None = None
    """The ``ReplayResult``, as ``cua replay`` prints it."""
    error: str | None = None
    """Why the request could not be carried out (state ``error``), or why the
    last approve or resume of it was refused."""
    requests: list[str] = Field(default_factory=list)
    """Every ``X-Request-Id`` that acted on the run, in order."""

    def view(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["links"] = {
            "self": f"/runs/{self.run_id}",
            "events": f"/runs/{self.run_id}/events",
        }
        return data


def error_body(code: str, message: str, request_id: str | None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}, "request_id": request_id}
