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
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Strategy = Literal["baseline_llm", "inter_cua_discovery", "inter_cua_replay"]
STRATEGIES: tuple[Strategy, ...] = ("baseline_llm", "inter_cua_discovery", "inter_cua_replay")

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
    and the declared outputs from the capability, so the agent is asked for
    exactly what replay returns."""

    goal: str
    entry: str = "/login"
    script: str | None = None
    """For ``--llm scripted``: a tool-call script (path relative to the
    repository root), with ``{{input}}`` placeholders filled from the task's inputs. Lets
    the harness run end to end without a key; its model numbers are zero and
    it proves nothing about a model."""


class BenchmarkTask(_Model):
    id: str
    name: str
    description: str = ""
    capability: str
    """Path of the approved capability ``inter_cua_replay`` runs."""
    goal: GoalSpec
    inputs: dict[str, str]
    inject: str | None = None
    """A mock-app failure mode, armed the same way for every strategy."""
    consent: bool = False
    """Consent is given for the risky step: replay gets a signed approval
    token, the baseline gets its risky actions approved. Same consent, in
    each strategy's own form."""
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
    cached: bool = False
    error_code: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    run_dir: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.thinking_tokens
