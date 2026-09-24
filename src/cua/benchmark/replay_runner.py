"""The approved capability, replayed: the inter-cua strategy after discovery.

Goes through ``cua.replay.runner.replay`` exactly as ``cua replay`` does:
approval gate, input validation, signed consent, idempotency, a fresh browser
per invocation, no model anywhere in the process.
"""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

from cua.artifact.schema import Capability
from cua.benchmark.environment import BenchEnv
from cua.benchmark.metrics import Commits, event_counts, result_label, score_replay
from cua.benchmark.models import BenchmarkTask, RunMetrics
from cua.evidence.logger import utc_now
from cua.policy import tokens
from cua.replay.engine import ReplayConfig
from cua.replay.invocation import ApprovalGrant, Invocation
from cua.replay.runner import replay

CONSENT_BY = "benchmark"


def consent(cap: Capability, task: BenchmarkTask, env: BenchEnv) -> str:
    """A signed approval token for exactly this task's inputs."""
    return tokens.mint(cap, env.tenant, task.inputs, approved_by=CONSENT_BY, key=env.signing_key)


def run_replay(
    task: BenchmarkTask,
    cap: Capability,
    path: Path,
    env: BenchEnv,
    *,
    session_id: str,
    repetition: int,
    token: str | None = None,
    idempotency_key: str | None = None,
    commits_earlier: int = 0,
) -> RunMetrics:
    before = env.commits_total()
    started_at = utc_now()
    started = time.monotonic()
    result = replay(
        path,
        tenant=env.tenant,
        policy=env.policy,
        invocation=Invocation(
            inputs=task.inputs,
            inject=task.inject,
            idempotency_key=idempotency_key,
            approval=ApprovalGrant(token=token) if token else None,
        ),
        runs_dir=env.runs_dir,
        config=ReplayConfig(),
        environ=env.environ,
    )
    wall = time.monotonic() - started
    if task.settle_s and not result.cached:
        time.sleep(task.settle_s)
    after = env.commits_total()
    commits = Commits(
        observed=None if before is None or after is None else after - before,
        earlier_in_group=commits_earlier,
    )
    match, detail = score_replay(task, result, commits)
    duplicates, unexpected, _ = commits.judge(task)
    # A cached answer ran nothing; its run_dir is the original run's.
    events = event_counts(None if result.cached else result.evidence.run_dir)
    return RunMetrics(
        session_id=session_id,
        task_id=task.id,
        strategy="inter_cua_replay",
        repetition=repetition,
        run_id=result.run_id,
        started_at=started_at,
        match=match,
        success=match == "exact",
        outcome=result_label(result),
        truth=f"{task.truth.kind}:{task.truth.code}" if task.truth.code else task.truth.kind,
        detail=detail,
        duration_s=round(wall, 3),
        estimated_cost_usd=Decimal(0),
        action_count=events["action.done"],
        recovery_count=0 if result.cached else len(result.recoveries),
        escalated=result.kind == "escalated"
        or (result.kind == "failure" and result.escalation_reason is not None),
        human_intervention=any(h.human_actions_count for h in result.handoffs),
        locator_slips=events["locator.slip"],
        policy_blocks=events["policy.block"],
        side_effect=result.side_effect,
        commits_observed=commits.observed,
        duplicate_side_effects=duplicates,
        unexpected_side_effects=unexpected,
        cached=result.cached,
        error_code=result.code if result.kind == "failure" else None,
        outputs={k: str(v) for k, v in result.outputs.items()},
        run_dir=result.evidence.run_dir,
    )
