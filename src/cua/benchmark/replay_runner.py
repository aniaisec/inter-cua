"""The approved capability, replayed: the inter-cua strategy after discovery.

Goes through ``cua.replay.runner.replay`` exactly as ``cua replay`` does:
approval gate, input validation, signed consent, idempotency, a fresh browser
per invocation, no model anywhere in the process. A workflow task goes through
``cua.workflow.runner.run`` exactly as ``cua workflow run`` does, which replays
each step the same way.
"""

from __future__ import annotations

import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

from cua.artifact.schema import Capability
from cua.benchmark.environment import BenchEnv
from cua.benchmark.metrics import (
    Commits,
    event_counts,
    result_label,
    score_replay,
    score_result,
)
from cua.benchmark.models import BenchmarkTask, RunMetrics
from cua.benchmark.subject import CAPABILITIES_DIR, Subject
from cua.evidence.logger import utc_now
from cua.policy import tokens
from cua.replay.engine import ReplayConfig
from cua.replay.invocation import ApprovalGrant, Invocation
from cua.replay.result import Failure
from cua.replay.runner import replay
from cua.workflow.planner import static_inputs
from cua.workflow.runner import WorkflowRequest
from cua.workflow.runner import run as run_workflow

CONSENT_BY = "benchmark"
EXPIRED_AGO_S = 600
"""An ``expired`` token was issued this long ago, for a minute."""
MESSAGE_CHARS = 240


def consent(cap: Capability, task: BenchmarkTask, env: BenchEnv) -> str:
    """A signed approval token for exactly this task's inputs."""
    return tokens.mint(cap, env.tenant, task.inputs, approved_by=CONSENT_BY, key=env.signing_key)


def token_for(cap: Capability, task: BenchmarkTask, env: BenchEnv) -> str | None:
    """The token the task's consent calls for (``ApprovalKind``), if any. A
    ``replayed`` token is a valid one: it is spent before the measured run."""
    kind = task.consent_kind
    if kind == "none":
        return None
    if kind == "wrong_inputs":
        other = {n: f"{v}9" for n, v in task.inputs.items()}  # 250.00 → 250.009
        return tokens.mint(cap, env.tenant, other, approved_by=CONSENT_BY, key=env.signing_key)
    if kind == "expired":
        return tokens.mint(
            cap,
            env.tenant,
            task.inputs,
            approved_by=CONSENT_BY,
            key=env.signing_key,
            ttl_s=60,
            now=time.time() - EXPIRED_AGO_S,
        )
    return consent(cap, task, env)


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
    allow_draft: bool = False,
) -> RunMetrics:
    """``allow_draft``: for a candidate repair under evaluation
    (``cua drift evaluate``), which is a draft until a person approves it."""
    config = ReplayConfig()
    if task.consent_kind == "replayed" and token is not None:
        # The earlier commit the token went into: not measured, and its
        # commit is not this run's.
        replay(
            path,
            tenant=env.tenant,
            policy=env.policy,
            invocation=Invocation(
                inputs=task.inputs,
                idempotency_key=f"{session_id}-{task.id}-{repetition}-spent",
                approval=ApprovalGrant(token=token),
            ),
            runs_dir=env.runs_dir,
            allow_draft=allow_draft,
            config=config,
            environ=env.environ,
        )
    before = env.app_counts()
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
        allow_draft=allow_draft,
        config=config,
        environ=env.environ,
    )
    wall = time.monotonic() - started
    if task.settle_s and not result.cached:
        time.sleep(task.settle_s)
    commits = Commits.between(before, env.app_counts(), earlier_in_group=commits_earlier)
    match, detail = score_replay(task, result, commits)
    if isinstance(result, Failure) and result.message:
        # Why it stopped, so a refusal can be told from another (a spent
        # token from a wrong one). Replay's messages carry no secret values.
        detail = f"{detail}: {result.message[:MESSAGE_CHARS]}"
    duplicates, unexpected, _ = commits.judge(task)
    # A cached answer ran nothing; its run_dir is the original run's.
    events = event_counts(None if result.cached else result.evidence.run_dir)
    return RunMetrics(
        session_id=session_id,
        task_id=task.id,
        category=task.category,
        strategy="inter_cua_replay",
        repetition=repetition,
        run_id=result.run_id,
        started_at=started_at,
        match=match,
        success=match == "exact",
        outcome=result_label(result),
        truth=_truth_label(task),
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
        forbidden_effects=commits.forbidden,
        cached=result.cached,
        error_code=result.code if result.kind == "failure" else None,
        outputs={k: str(v) for k, v in result.outputs.items()},
        run_dir=result.evidence.run_dir,
    )


def run_workflow_replay(
    task: BenchmarkTask,
    subject: Subject,
    env: BenchEnv,
    *,
    session_id: str,
    repetition: int,
    idempotency_key: str,
    commits_earlier: int = 0,
    capabilities_dir: Path = CAPABILITIES_DIR,
) -> RunMetrics:
    """A workflow task, run as ``cua workflow run`` runs it. Consent is a
    token per committing step, for the inputs that step will get."""
    assert subject.plan is not None and subject.path is not None
    approvals: dict[str, ApprovalGrant] = {}
    if task.consent_kind == "valid":
        for step in subject.plan.steps:
            inputs = static_inputs(step, task.inputs)
            if step.needs_consent and inputs is not None:
                token = tokens.mint(
                    step.capability, env.tenant, inputs, approved_by=CONSENT_BY, key=env.signing_key
                )
                approvals[step.id] = ApprovalGrant(token=token)
    inject = {}
    if task.inject:
        inject = {task.inject_step or subject.plan.steps[0].id: task.inject}
    before = env.app_counts()
    started_at = utc_now()
    started = time.monotonic()
    result = run_workflow(
        subject.path,
        tenant=env.tenant,
        policy=env.policy,
        request=WorkflowRequest(
            inputs=task.inputs,
            idempotency_key=idempotency_key,
            approvals=approvals,
            inject=inject,
        ),
        capabilities_dir=capabilities_dir,
        runs_dir=env.runs_dir,
        config=ReplayConfig(),
        environ=env.environ,
    )
    wall = time.monotonic() - started
    if task.settle_s and not result.cached:
        time.sleep(task.settle_s)
    commits = Commits.between(before, env.app_counts(), earlier_in_group=commits_earlier)
    outputs = {k: str(v) for k, v in result.outputs.items()}
    match, detail = score_result(task, result.kind, result.code, outputs, commits)
    duplicates, unexpected, _ = commits.judge(task)
    events: Counter[str] = Counter()
    for ran in result.steps:
        if not ran.cached:
            events.update(event_counts(ran.run_dir))
    label = f"{result.kind}:{result.code}" if result.code else result.kind
    return RunMetrics(
        session_id=session_id,
        task_id=task.id,
        category=task.category,
        strategy="inter_cua_replay",
        repetition=repetition,
        run_id=result.workflow_run_id,
        started_at=started_at,
        match=match,
        success=match == "exact",
        outcome=label,
        truth=_truth_label(task),
        detail=f"{detail}; steps: " + ", ".join(f"{s.id}={s.kind}" for s in result.steps),
        duration_s=round(wall, 3),
        estimated_cost_usd=Decimal(0),
        action_count=events["action.done"],
        escalated=result.kind == "escalated",
        locator_slips=events["locator.slip"],
        policy_blocks=events["policy.block"],
        side_effect=result.side_effect,
        commits_observed=commits.observed,
        duplicate_side_effects=duplicates,
        unexpected_side_effects=unexpected,
        forbidden_effects=commits.forbidden,
        cached=result.cached,
        error_code=result.code if result.kind == "failure" else None,
        outputs=outputs,
        run_dir=result.run_dir,
    )


def _truth_label(task: BenchmarkTask) -> str:
    truth = task.truth
    return f"{truth.kind}:{truth.code}" if truth.code else truth.kind
