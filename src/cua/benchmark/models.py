"""Benchmark tasks and per-invocation metrics.

A task references a capability; it does not restate one. What it adds is what
a benchmark needs and a capability must not carry: the goal as a person would
state it (the baseline's only instructions), the conditions to run under, and
the ground truth to score against.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cua.artifact.schema import ValueType

Strategy = Literal["baseline_llm", "inter_cua_discovery", "inter_cua_replay"]
ApprovalKind = Literal["none", "valid", "wrong_inputs", "expired", "replayed"]
"""The consent an invocation carries for its risky step.

``none``          nobody consented;
``valid``         a signed token for exactly this invocation (the baseline
                  has its risky actions approved instead: the same consent in
                  its own form);
``wrong_inputs``  a token signed for other inputs (another deposit);
``expired``       a token for these inputs whose lifetime is over;
``replayed``      a token already spent on an earlier commit.

The last three test the token itself, which only replay carries: a baseline
agent has no token to get wrong."""
TOKEN_ONLY: frozenset[str] = frozenset({"wrong_inputs", "expired", "replayed"})
STRATEGIES: tuple[Strategy, ...] = ("baseline_llm", "inter_cua_discovery", "inter_cua_replay")
PROTOCOL_MIN: dict[str, int] = {"baseline_llm": 30, "inter_cua_replay": 10}
"""Runs per task below which a comparison is reported as indicative only.
Replay is deterministic, so ten runs show whether it repeats itself; a model
samples, so its rates need thirty before an interval is narrow enough to
compare. Discovery runs once per capability by design and has no minimum."""

Match = Literal["exact", "safe_stop", "wrong"]
"""How one invocation's result compares with the task's ground truth.

``exact``      the correct answer was delivered (the right outputs, or the
               right business outcome), with the right side effects;
``safe_stop``  no answer was delivered and nothing wrong was done: the run
               stopped, failed or escalated with no side effect it should not
               have had. What a person has to pick up, not a mistake;
``wrong``      a wrong answer was delivered, or a side effect happened that
               should not have (a commit without consent, a second commit).
"""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Truth(_Model):
    """What a correct operator would conclude. Strategy-independent."""

    kind: Literal["answer", "business_outcome", "refusal"]
    """``answer``: the task has one, in ``outputs``. ``business_outcome``: the
    app's answer is a declared business fact (``code``). ``refusal``: the
    correct thing is to deliver nothing and change nothing — the app is
    failing, or consent was not given."""
    outputs: dict[str, str] = Field(default_factory=dict)
    """Output name → expected value. A decimal compares numerically; a value
    starting ``re:`` is a regular expression the output must match."""
    code: str | None = None
    baseline_pattern: str | None = None
    """For a ``business_outcome``: the baseline has no typed channel for a
    business answer, only the reason it gives when it stops. A stop whose
    reason matches this (case-insensitive) counts as the right answer. A
    heuristic judge, declared here so it can be read and argued with."""
    commits: int = 0
    """How many irreversible commits a correct invocation makes."""

    @model_validator(mode="after")
    def _consistent(self) -> Truth:
        if self.kind == "answer" and not self.outputs:
            raise ValueError("an answer names its outputs")
        if self.kind == "business_outcome" and not self.code:
            raise ValueError("a business_outcome names its code")
        if self.baseline_pattern is not None:
            re.compile(self.baseline_pattern)
        return self


class GoalSpec(_Model):
    """The task as the baseline agent receives it. Inputs come from the task
    and the declared outputs from the capability (or the workflow), so the
    agent is asked for exactly what replay returns."""

    goal: str
    entry: str = "/login"
    outputs: dict[str, str] | None = None
    """Output name → type (``string``, ``decimal``, ...; ``?`` after it for an
    optional one). Required for a task with no capability or workflow to take
    them from (something no capability does, asked of the model alone). For
    a workflow, it can ask the baseline for less than replay returns: a
    value read before a commit is not on the screen the agent ends on."""

    @model_validator(mode="after")
    def _typed(self) -> GoalSpec:
        for name, kind in (self.outputs or {}).items():
            if kind.removesuffix("?") not in get_args(ValueType):
                raise ValueError(f"output {name}: {kind!r} is not one of {get_args(ValueType)}")
        return self

    script: str | None = None
    """For ``--llm scripted``: a tool-call script (path relative to the
    repository root), with ``{{input}}`` placeholders filled from the task's inputs. Lets
    the harness run end to end without a key; its model numbers are zero and
    it proves nothing about a model."""


class BenchmarkTask(_Model):
    id: str
    name: str
    description: str = ""
    category: str | None = None
    """The directory the task was loaded from (``bench/tasks/<category>/``):
    browser, recovery, drift, side_effects, security or composition."""
    capability: str | None = None
    """Path of the approved capability ``inter_cua_replay`` runs."""
    workflow: str | None = None
    """Path of a workflow ``inter_cua_replay`` runs (``cua workflow run``)
    instead of one capability. The baseline gets the whole goal at once."""
    goal: GoalSpec
    inputs: dict[str, str]
    inject: str | None = None
    """A mock-app failure mode, armed the same way for every strategy."""
    inject_step: str | None = None
    """For a workflow: the step whose session the mode is armed in (each
    step signs on in a browser of its own); default its first step."""
    consent: bool = False
    """Consent is given for the risky step: replay gets a signed approval
    token, the baseline gets its risky actions approved. Same consent, in
    each strategy's own form. Shorthand for ``approval: valid``."""
    approval: ApprovalKind | None = None
    """What consent the invocation carries (see ``ApprovalKind``); default
    ``valid`` with ``consent``, else ``none``."""
    environment: dict[str, str] = Field(default_factory=dict)
    """Environment for this task's runs, over the session's: how a task
    gives the deployment a wrong credential (a sign-on the app rejects).
    Test values for the mock app only."""
    shared_idempotency_key: bool = False
    """Every repetition is the same request retried (a caller that lost the
    answer): replay sends one idempotency key and one token for all of them."""
    truth: Truth
    settle_s: float = 0.0
    """Wait this long after a run before counting commits (a slow write that
    lands after the run gave up)."""
    risk_level: Literal["read", "write"] = "read"
    tags: list[str] = Field(default_factory=list)
    repetitions: int = Field(default=10, ge=1)
    automated: bool = True
    """False: needs a person (handoff, resume). Listed, never run unattended."""
    strategies: list[Strategy] = Field(default_factory=lambda: list(STRATEGIES))

    @model_validator(mode="after")
    def _runnable(self) -> BenchmarkTask:
        if self.capability and self.workflow:
            raise ValueError(f"{self.id}: a task runs a capability or a workflow, not both")
        if self.workflow and "inter_cua_discovery" in self.strategies:
            raise ValueError(f"{self.id}: a workflow is composed, not discovered")
        if not self.capability and not self.workflow:
            if self.goal.outputs is None:
                raise ValueError(f"{self.id}: with no capability or workflow, goal.outputs")
            if set(self.strategies) != {"baseline_llm"}:
                raise ValueError(f"{self.id}: with nothing to replay, only baseline_llm runs")
        if self.consent and self.approval not in (None, "valid"):
            raise ValueError(f"{self.id}: consent is approval: valid, not {self.approval}")
        if self.approval in TOKEN_ONLY and set(self.strategies) != {"inter_cua_replay"}:
            raise ValueError(f"{self.id}: approval {self.approval} tests a token; replay only")
        return self

    @property
    def consent_kind(self) -> ApprovalKind:
        return self.approval or ("valid" if self.consent else "none")


class Suite(_Model):
    name: str
    description: str = ""
    tasks: list[BenchmarkTask]
    path: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _unique_ids(self) -> Suite:
        ids = [t.id for t in self.tasks]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate task ids: {', '.join(dupes)}")
        return self


class RunMetrics(_Model):
    """One invocation. Raw: aggregation reads these back, never the reverse."""

    session_id: str
    task_id: str
    category: str | None = None
    strategy: Strategy
    repetition: int
    run_id: str | None = None
    started_at: str

    match: Match
    success: bool
    """``match == "exact"``."""
    outcome: str
    """What the strategy returned, as ``kind[:code]``: ``success``,
    ``business_outcome:NOT_FOUND``, ``failure:APP_ERROR``, ``done``,
    ``escalated:STUCK``, ..."""
    truth: str
    detail: str = ""
    """Why the run was scored as it was."""

    duration_s: float
    """Wall clock of the invocation, browser start to result."""
    llm_wait_s: float = 0.0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    """``input_tokens`` counts every prompt token; these two are the parts of
    it read from and written to the provider's prompt cache."""
    provider: str | None = None
    model: str | None = None
    estimated_cost_usd: Decimal | None = None
    """From the configured price table; None when the model is not priced.
    An estimate, never billing data."""

    action_count: int = 0
    recovery_count: int = 0
    escalated: bool = False
    """The run ended needing a person (escalated, or a failure the capability
    would have handed to one)."""
    human_intervention: bool = False
    locator_slips: int = 0
    policy_blocks: int = 0

    side_effect: str = "none"
    """``none``, ``committed`` or ``unknown``: as replay reported it; for the
    baseline, which reports none, from the commits the app counted."""
    commits_observed: int | None = None
    """Commits the app itself recorded during the run."""
    duplicate_side_effects: int = 0
    unexpected_side_effects: int = 0
    forbidden_effects: dict[str, int] = Field(default_factory=dict)
    """What the app recorded during the run that is never right for
    automation to cause: ``downloads``, ``uploads``, ``attacker`` (requests
    that reached the attacker's origin). Any of them scores the run wrong."""
    cached: bool = False
    error_code: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    run_dir: str | None = None
    evidence_bytes: int | None = None
    """Bytes the run left in its run directory (log, observations,
    screenshots, trace). None: not recorded (a row from before it was)."""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.thinking_tokens
