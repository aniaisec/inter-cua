"""What the engine needs from whoever can hand a run to a human.

The engine knows nothing about files, operator consoles or debugging ports.
When a fault is one a person should be asked about, it describes the situation
(``HandoffRequest``), asks a ``Handoff`` to open a request, and waits on the
ticket for a ``Decision``:

``HandBack``    a human is done (or approved, or asked for the step again) and
                the automation may carry on — from wherever the screen proves
                the run has got to, which the engine finds out for itself.
``Abort``       a human stopped the run, or the request expired unanswered.
``Unanswered``  nobody picked the request up while this process waited. The
                session is left open and the caller gets ``escalated`` with a
                resume token; ``cua resume`` picks the run up later.

``cua.escalation`` implements this over a file queue, a control lease and an
operator console; the unit tests implement it in memory.
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from cua.replay.result import EscalationReason, SideEffect

Option = Literal["take_control", "resume", "retry_step", "approve", "abort"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ResumeChoice(_Model):
    """A place the operator may ask the run to carry on from. Offered, never
    trusted: the checkpoint it names still has to hold on the screen."""

    step_id: str
    """The step to carry on with (``done``: only the outputs are left)."""
    after_checkpoint: str | None
    label: str


class HandoffRequest(_Model):
    """Everything a person needs to decide, with nothing they must not see:
    the screenshot is masked, the excerpt scrubbed, before either is written."""

    step_id: str | None
    reason: EscalationReason
    code: str
    """The fault that raised it (``LOCATOR_UNRESOLVED``, ``POLICY_BLOCKED``, ...)."""
    message: str
    expected: str = ""
    observed: str = ""
    """A compact, scrubbed excerpt of the accessibility tree."""
    screenshot: str | None = None
    """Relative to the run directory; masked when it was taken."""
    side_effect: SideEffect = "none"
    options: list[Option]
    resume_points: list[ResumeChoice] = Field(default_factory=list)
    attempt: int = 1
    """2 and up: the previous handback left the screen where no checkpoint holds."""


class Ticket(_Model):
    request_id: str
    resume_token: str
    operator_url: str | None = None
    intervention: str
    """The request as written, relative to the run directory."""


class HandBack(_Model):
    kind: Literal["hand_back"] = "hand_back"
    by: str
    resume_at: str | None = None
    """A step id the operator picked; checked against its checkpoint first."""
    retry: bool = False
    """Perform the step that stopped the run again (never an irreversible one
    that may already have happened)."""
    approved: bool = False
    """The operator consents to the step that needed approval."""


class Abort(_Model):
    kind: Literal["abort"] = "abort"
    by: str
    why: str = ""


class Unanswered(_Model):
    kind: Literal["unanswered"] = "unanswered"
    why: str


Decision: TypeAlias = Annotated[HandBack | Abort | Unanswered, Field(discriminator="kind")]


class Handoff(Protocol):
    def open(self, request: HandoffRequest) -> Ticket:
        """Publish a request and give up the controls (automation → paused)."""
        ...

    def wait(self, ticket: Ticket) -> Decision:
        """Block until the request is answered, or this process stops waiting."""
        ...

    def resumed(self, ticket: Ticket, *, checkpoint: str | None, next_step: str) -> None:
        """The screen proved where the run is; the automation holds the
        controls again (resuming → automation)."""
        ...

    def human_actions(self, ticket: Ticket) -> int:
        """How many things a person did in the browser under this request."""
        ...
