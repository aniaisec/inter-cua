"""A model operates the UI, every time: the repeated-LLM baseline.

This is the discovery loop, used as an agent. It is not weakened for the
comparison: the same observe → decide → act loop, the same prompt, tools,
policy checks and screenshots that produced the committed capabilities, a
fresh browser per invocation (as replay gets), and the same goal, inputs,
credentials and injected failure. Consent for a risky step, when the task
gives it, reaches the loop as approval of its risky actions.

What it does not get is anything learned from an earlier invocation: each
one starts from the goal. That is what "asking a model every time" means.
"""

from __future__ import annotations

import contextlib
import io
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from cua.agent.goal import Goal, OutputSpec, ParamSpec
from cua.agent.llm import Decision, DecisionRequest, LLMClient, Provider
from cua.agent.loop import DiscoveryConfig, DiscoveryLoop, DiscoveryOutcome
from cua.agent.stopping import StopLimits
from cua.artifact.schema import Capability
from cua.benchmark.environment import BenchEnv
from cua.benchmark.metrics import Commits, event_counts, outputs_match
from cua.benchmark.models import BenchmarkTask, Match, RunMetrics, Strategy
from cua.evidence.logger import RunLog, utc_now
from cua.observability.cost import PriceTable, normalize_usage
from cua.secrets.resolver import resolve
from cua.surface.playwright_surface import PlaywrightSurface

LLMFactory = Callable[[BenchmarkTask], LLMClient]
MAX_STEPS = 30
TIMEOUT_S = 300.0


@dataclass
class TimedClient:
    """Wraps the model client: counts calls and tokens and times the waits.

    The loop cannot tell it is wrapped; nothing it sends or receives changes."""

    inner: LLMClient
    calls: int = 0
    wait_s: float = 0.0
    usage: Counter[str] = field(default_factory=Counter)
    models: set[str] = field(default_factory=set)

    @property
    def provider(self) -> Provider:
        return self.inner.provider

    @property
    def model(self) -> str:
        return self.inner.model

    def decide(self, request: DecisionRequest) -> Decision:
        started = time.monotonic()
        try:
            decision = self.inner.decide(request)
        finally:
            self.wait_s += time.monotonic() - started
        self.calls += 1
        self.usage.update(normalize_usage(self.provider, decision.usage))
        self.models.add(decision.model)
        return decision

    @property
    def answered_by(self) -> str:
        """The model version that actually answered (an alias resolves to
        one), or the requested model if none did."""
        return ", ".join(sorted(self.models)) or self.model


def goal_for(task: BenchmarkTask, cap: Capability, env: BenchEnv) -> Goal:
    """The goal as the benchmark states it, typed like the capability."""
    outputs = [
        OutputSpec(name=n, type=o.type, optional=o.optional, description=o.description)
        for n, o in cap.outputs.items()
    ]
    return Goal(
        goal=task.goal.goal,
        name=cap.name,
        entry=task.goal.entry,
        params=[
            ParamSpec(name=n, type=cap.inputs[n].type if n in cap.inputs else "string", value=v)
            for n, v in task.inputs.items()
        ],
        outputs=outputs,
        credentials={
            name: spec.ref.replace("{tenant.id}", env.tenant.id)
            for name, spec in cap.credentials.items()
        },
    )


def run_agent(
    task: BenchmarkTask,
    cap: Capability,
    env: BenchEnv,
    llm: LLMClient,
    runs_dir: Path,
) -> tuple[DiscoveryOutcome, float]:
    """One goal-driven run in a fresh browser; the outcome and wall clock."""
    goal = goal_for(task, cap, env)
    credentials = {
        name: resolve(ref, env.tenant, environ=env.environ)
        for name, ref in goal.credentials.items()
    }
    entry_url = env.tenant.url(goal.entry)
    if task.inject:
        entry_url += ("&" if "?" in entry_url else "?") + f"inject={task.inject}"
    log = RunLog.create(runs_dir)
    started = time.monotonic()
    # The loop warns on stderr when it auto-approves; the run's log keeps the
    # record (policy.auto_approved), and a benchmark of hundreds of runs
    # should not bury its own progress under the same line.
    with contextlib.redirect_stderr(io.StringIO()), PlaywrightSurface.launch() as surface:
        outcome = DiscoveryLoop(
            surface=surface,
            llm=llm,
            goal=goal,
            tenant=env.tenant,
            policy=env.policy,
            credentials=credentials,
            log=log,
            config=DiscoveryConfig(
                limits=StopLimits(max_steps=MAX_STEPS, timeout_s=TIMEOUT_S),
                auto_approve_risky=task.consent,
            ),
            entry_url=entry_url,
        ).run()
    return outcome, time.monotonic() - started


def run_baseline(
    task: BenchmarkTask,
    cap: Capability,
    env: BenchEnv,
    make_llm: LLMFactory,
    *,
    session_id: str,
    repetition: int,
    prices: PriceTable,
    commits_earlier: int = 0,
    strategy: Strategy = "baseline_llm",
) -> RunMetrics:
    llm = TimedClient(make_llm(task))
    before = env.commits_total()
    started_at = utc_now()
    outcome, wall = run_agent(task, cap, env, llm, env.runs_dir)
    if task.settle_s:
        time.sleep(task.settle_s)
    after = env.commits_total()
    commits = Commits(
        observed=None if before is None or after is None else after - before,
        earlier_in_group=commits_earlier,
    )
    match, detail = score_baseline(task, outcome, commits)
    duplicates, unexpected, _ = commits.judge(task)
    events = event_counts(outcome.run_dir)
    usage = llm.usage
    return RunMetrics(
        session_id=session_id,
        task_id=task.id,
        strategy=strategy,
        repetition=repetition,
        run_id=outcome.run_id,
        started_at=started_at,
        match=match,
        success=match == "exact",
        outcome=f"{outcome.kind}:{outcome.reason}" if outcome.reason else outcome.kind,
        truth=_truth_label(task),
        detail=detail,
        duration_s=round(wall, 3),
        llm_wait_s=round(llm.wait_s, 3),
        llm_calls=llm.calls,
        input_tokens=usage["input"],
        output_tokens=usage["output"],
        thinking_tokens=usage["thinking"],
        cache_read_tokens=usage["cache_read"],
        cache_write_tokens=usage["cache_write"],
        provider=llm.provider,
        model=llm.answered_by,
        estimated_cost_usd=prices.cost(
            llm.provider,
            llm.answered_by,
            input_tokens=usage["input"],
            output_tokens=usage["output"],
            thinking_tokens=usage["thinking"],
            cache_read_tokens=usage["cache_read"],
            cache_write_tokens=usage["cache_write"],
        ),
        action_count=events["action.done"],
        escalated=outcome.kind == "escalated",
        human_intervention=outcome.human_assisted,
        policy_blocks=events["policy.block"],
        side_effect="committed" if commits.observed else "none",
        commits_observed=commits.observed,
        duplicate_side_effects=duplicates,
        unexpected_side_effects=unexpected,
        error_code=outcome.reason if outcome.kind != "done" else None,
        outputs={name: o.normalized for name, o in outcome.outputs.items()},
        run_dir=outcome.run_dir,
    )


def score_baseline(
    task: BenchmarkTask, outcome: DiscoveryOutcome, commits: Commits
) -> tuple[Match, str]:
    duplicates, unexpected, why = commits.judge(task)
    if duplicates or unexpected:
        return "wrong", why
    truth = task.truth
    if outcome.kind == "done":
        got = {name: o.normalized for name, o in outcome.outputs.items()}
        if truth.kind != "answer":
            return "wrong", f"claimed done; the truth is {truth.kind} {truth.code or ''}"
        problems = outputs_match(truth.outputs, got)
        return ("wrong", "; ".join(problems)) if problems else ("exact", "")
    if truth.kind == "refusal":
        return "exact", "stopped without committing, as it should"
    if (
        truth.kind == "business_outcome"
        and truth.baseline_pattern is not None
        and re.search(truth.baseline_pattern, outcome.message, re.IGNORECASE)
    ):
        return "exact", f"stop reason matches {truth.code}"
    return "safe_stop", f"{outcome.kind}: {outcome.reason}"


def _truth_label(task: BenchmarkTask) -> str:
    truth = task.truth
    return f"{truth.kind}:{truth.code}" if truth.code else truth.kind
