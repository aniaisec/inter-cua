"""The canonical event vocabulary: one name for each thing a run can do.

A run directory already holds everything that happened, in the words of
whichever part of the system wrote it: the replay engine logs ``step.passed``
and ``replay.failed``, the discovery loop ``agent.decision`` and ``action.done``,
the handoff controller writes ``state_transitions.jsonl``, the operator
console ``human_actions.jsonl``. Those files stay the record. What this module
defines is the one vocabulary they are all read into (``cua.observability
.recorder``), so a question such as "did a human intervene?" has one answer
however the run was made.

Canonical events are derived, never written in place of the log: old evidence
reads the same way as new, and there is no second writer to drift from the
first. Each event keeps the legacy name it came from (``source``) and its
position in the source file, so every derived fact can be traced back to a
line of evidence.

Nothing here holds a secret. Events are built from files that were scrubbed
when they were written, and ``attrs`` carries only the fields a question needs
(no screenshots, no observation trees, no typed text).
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

EventType = Literal[
    "run.started",
    "run.completed",
    "run.failed",
    "run.escalated",
    "step.started",
    "step.action",
    "step.completed",
    "step.failed",
    "llm.started",
    "llm.completed",
    "locator.resolved",
    "locator.failed",
    "locator.drift",
    "recovery.started",
    "recovery.completed",
    "recovery.exhausted",
    "policy.checked",
    "policy.blocked",
    "human.handoff",
    "human.action",
    "human.resumed",
    "human.aborted",
    "side_effect.detected",
    "side_effect.committed",
    "side_effect.unknown",
]
EVENT_TYPES: tuple[str, ...] = get_args(EventType)


class Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: EventType
    timestamp: str
    """ISO-8601 UTC, millisecond precision, as the source line has it."""
    run_id: str
    invocation_id: str
    tenant_id: str | None = None
    capability_id: str | None = None
    capability_version: int | None = None
    step_id: str | None = None
    """The capability step (replay) or ``turn:<n>`` (discovery)."""
    source: str
    """``<file>:<legacy name>``, e.g. ``log.jsonl:replay.failed``."""
    source_seq: int | None = None
    """The line's ``seq`` in its file, where the file numbers its lines."""
    attrs: dict[str, Any] = Field(default_factory=dict)
    derived: bool = False
    """True when no single source line says this; it was inferred from the
    run's shape (an ``llm.started`` from a call that logged only its end, a
    ``side_effect.unknown`` from the result). ``attrs['basis']`` says how."""
