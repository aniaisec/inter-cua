"""What drift is, as data: the event, its classification, and the candidate
repair a drift can lead to.

**Drift** is the app no longer matching what a capability recorded about it:
a control under another name, in another place or frame, or two where there
was one; a checkpoint that no longer holds; an output no longer where it was
read. It is a fact about the app, not a fault of the run. A run can survive
drift (a weaker rung of the ladder still named the control: ``fatal`` is
false) or be stopped by it (``fatal``).

Drift events are **derived** from run directories, like the canonical events
they are built on (``cua.observability.recorder``); nothing new is written
during a run. Old evidence is classified exactly like new.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

DriftKind = Literal[
    "CONTROL_RENAMED",
    "CONTROL_MISSING",
    "CONTROL_AMBIGUOUS",
    "LAYOUT_CHANGED",
    "FRAME_CHANGED",
    "CHECKPOINT_CHANGED",
    "OUTPUT_CHANGED",
    "NAVIGATION_CHANGED",
]
"""
``CONTROL_RENAMED``     the control is where it was, with the role it had, under
                        another name: the name rung found nothing and the
                        pixel rung found exactly one control of that role.
``CONTROL_MISSING``     no rung found it, and nothing at its recorded place.
``CONTROL_AMBIGUOUS``   a rung found more than one control and none found one.
``LAYOUT_CHANGED``      it was found, but not by the rung it was recorded on:
                        its label or grid position moved (the run went on).
``FRAME_CHANGED``       the frame the ladder is scoped to is gone, or the
                        control is now in another frame.
``CHECKPOINT_CHANGED``  a step landed, and the screen it landed on no longer
                        satisfies the checkpoint recorded after it.
``OUTPUT_CHANGED``      an output could not be read where it was recorded, or
                        was found by a weaker rung.
``NAVIGATION_CHANGED``  a step landed somewhere else: the checkpoint's
                        location condition does not hold.
"""
DRIFT_KINDS: tuple[str, ...] = get_args(DriftKind)

REPAIRABLE: frozenset[str] = frozenset({"CONTROL_RENAMED"})
"""Kinds a candidate can be proposed for from the evidence alone, without a
model: the failure screen shows the same control at the same place, so the
repair is its new name. The rest need a person, or a new discovery run."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DriftEvent(_Model):
    capability: str
    version: int
    tenant_id: str
    app_family: str
    step_id: str
    """The capability step, or ``outputs.<name>`` for an output."""
    expected_locator_rung: str
    """The rung the control was recorded on (for a checkpoint: ``-``)."""
    observed_rungs: list[str]
    """What each rung found on this run, in ladder order: ``role_name=0``,
    ``bbox=1 untrusted``, ``near_text=refused (...)``."""
    reason: str
    evidence_ref: str | None
    """The screen the drift was seen on (an observation file), else the log
    line that says it."""
    kind: DriftKind
    fatal: bool
    """The run stopped on it (it failed or went to a person)."""
    resolved_rung: str | None = None
    """For drift the run survived: the rung that did name the control."""
    run_id: str
    invocation_id: str
    at: str
    injected: str | None = None
    """A failure injected into the mock app for this run, if any: drift
    produced on purpose, by a test or a benchmark."""
    basis: str
    """How it was classified: from the rung attempts the run logged, from
    those attempts read back out of the failure message (runs logged before
    the attempts were data), or from the recorded screen as well."""


class RunDrift(_Model):
    """One replay run, read for drift."""

    run_id: str
    invocation_id: str
    capability: str
    version: int
    tenant_id: str
    app_family: str
    injected: str | None = None
    reached_app: bool
    """At least one step started. A request refused before any browser
    (a draft, bad inputs, no consent) cannot see drift and is not counted."""
    lookups: dict[str, int] = Field(default_factory=dict)
    """Recorded rung → controls looked up on it during the run."""
    events: list[DriftEvent] = Field(default_factory=list)


# -- candidates -----------------------------------------------------------------------

CheckResult = Literal["pass", "fail", "attention", "info"]


class Check(_Model):
    """A security or safety property of a candidate, checked offline.
    ``attention`` does not block approval; it tells the reviewer where to
    look hardest. ``fail`` blocks it."""

    id: str
    result: CheckResult
    detail: str


class Base(_Model):
    """The version the drift was seen on, which the candidate repairs."""

    version: int
    artifact_hash: str
    status: str
    path: str


class Change(_Model):
    step_id: str
    kind: Literal["locator_added"] = "locator_added"
    before: list[dict[str, Any]]
    after: list[dict[str, Any]]
    added: list[dict[str, Any]]
    """The rungs put in front of the recorded ones. The recorded rungs stay
    behind them, so a tenant still running the old screen is still served."""


class Control(_Model):
    """The control the repair names, as the failure screen showed it."""

    role: str
    name: str
    frame: str | None
    bbox: dict[str, float] | None
    found_by: str
    """Why it is taken to be the same control."""
    recorded_name: str | None = None
    corroborated_by: str | None = None
    """A person who, holding the controls during that run's escalation,
    acted on a control with this role and name."""


class SideResult(_Model):
    runs: int
    exact: int
    safe_stop: int
    wrong: int
    outcomes: list[str]
    run_ids: list[str | None]


class TaskResult(_Model):
    task_id: str
    name: str
    tags: list[str]
    inject: str | None
    reproduces_drift: bool
    """The task injects the same fault the drift was seen under."""
    incumbent: SideResult
    candidate: SideResult


class Gate(_Model):
    id: str
    passed: bool
    detail: str


class Evaluation(_Model):
    at: str
    artifact_hash: str
    """The candidate content that was evaluated. An evaluation of other
    content is not evidence about this content."""
    suite: str
    task_filter: list[str] = Field(default_factory=list)
    """The tasks asked for, when not every task for the capability ran."""
    repetitions: int
    runs_dir: str
    tasks: list[TaskResult]
    checks: list[Check]
    gates: list[Gate]
    passed: bool


class Rejection(_Model):
    by: str
    at: str
    reason: str


class CandidateRecord(_Model):
    """``candidate.json``: everything a reviewer needs to decide on a repair."""

    schema_version: Literal["candidate-1"] = "candidate-1"
    name: str
    version: int
    capability_id: str
    artifact_hash: str
    created_at: str
    base: Base
    drift: list[DriftEvent]
    """The drift the candidate answers; the first is the one it was built from."""
    change: Change
    control: Control
    evidence: list[str]
    """Copies kept beside the candidate, relative to its directory."""
    checks: list[Check]
    evaluation: Evaluation | None = None
    rejection: Rejection | None = None
