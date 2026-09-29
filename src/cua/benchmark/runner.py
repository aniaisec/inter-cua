"""One benchmark session: tasks by strategies by repetitions, recorded as it goes.

Runs are sequential. The app's commit counter is read before and after each
run, and that only attributes a commit to a run if nothing else is running.
Each row is appended the moment its run ends, so an interrupted session keeps
what it measured.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from cua.agent.llm import LLMClient, ScriptedClient, generation_settings, select_client
from cua.agent.prompts import prompt_version
from cua.agent.script import Script
from cua.artifact.recorder import RecordError, record
from cua.artifact.store import save
from cua.benchmark.baseline_runner import run_baseline
from cua.benchmark.economics import evidence_bytes
from cua.benchmark.environment import BenchEnv, app_version, bench_env, mockapp
from cua.benchmark.metrics import human_fields
from cua.benchmark.models import BenchmarkTask, RunMetrics, Strategy
from cua.benchmark.replay_runner import run_replay, run_workflow_replay, token_for
from cua.benchmark.storage import (
    REPORTS_DIR,
    SessionInfo,
    append_run,
    append_session,
)
from cua.benchmark.subject import Subject, subject_for, task_version
from cua.evidence.logger import new_run_id, utc_now
from cua.observability.cost import PRICING_PATH, PriceTable

BENCH_RUNS = Path("bench/runs")
Progress = Callable[[RunMetrics], None]


class SkipStrategy(Exception):
    """This strategy cannot run this task as configured (no script, say)."""


@dataclass
class Plan:
    tasks: list[BenchmarkTask]
    strategies: list[Strategy]
    suite: str
    llm: str = "scripted"
    """``scripted``, ``auto``, ``anthropic`` or ``gemini``."""
    model: str | None = None
    repetitions: int | None = None
    """Overrides every task's own count."""
    repetitions_by_strategy: dict[str, int] = field(default_factory=dict)
    """Overrides ``repetitions`` for one strategy: a stochastic baseline needs
    more runs than a deterministic replay to say the same thing."""
    discovery_repetitions: int = 1
    prices: PriceTable = field(default_factory=PriceTable)
    reports_dir: Path = REPORTS_DIR
    runs_root: Path = BENCH_RUNS

    def count_for(self, task: BenchmarkTask, strategy: Strategy) -> int:
        """Runs of ``task`` under ``strategy``: the strategy's own override,
        else the session's, else the task's; discovery at most
        ``discovery_repetitions`` whatever the rest say."""
        count = self.repetitions_by_strategy.get(strategy) or self.repetitions or task.repetitions
        if strategy == "inter_cua_discovery":
            count = min(count, self.discovery_repetitions)
        return count


def llm_factory(llm: str, model: str | None) -> Callable[[BenchmarkTask], LLMClient]:
    """A fresh client per run, so no state carries from one invocation to
    the next. ``scripted`` plays the task's script with its inputs filled in."""

    def make(task: BenchmarkTask) -> LLMClient:
        if llm != "scripted":
            return select_client(llm, model)
        if task.goal.script is None:
            raise SkipStrategy(f"{task.id} has no script for --llm scripted")
        text = Path(task.goal.script).read_text(encoding="utf-8")
        for name, value in task.inputs.items():
            text = text.replace("{{" + name + "}}", value)
        return ScriptedClient(Script.model_validate(yaml.safe_load(text)))

    return make


def run_session(plan: Plan, progress: Progress | None = None) -> SessionInfo:
    session_id = "bench_" + new_run_id().removeprefix("run_")
    runs_dir = plan.runs_root / session_id
    make_llm = llm_factory(plan.llm, plan.model)
    subjects = {t.id: subject_for(t) for t in plan.tasks}
    asks_model = any(s != "inter_cua_replay" for s in plan.strategies)
    provider, model = _provider(plan) if asks_model else (None, plan.model)
    info = SessionInfo(
        session_id=session_id,
        started_at=utc_now(),
        suite=plan.suite,
        tasks=[t.id for t in plan.tasks],
        strategies=list(plan.strategies),
        repetitions=plan.repetitions,
        repetitions_by_strategy=dict(plan.repetitions_by_strategy),
        llm=plan.llm,
        model=model,
        llm_settings=generation_settings(provider) if provider else {},
        prompt_version=prompt_version() if asks_model else None,
        app_version=app_version(),
        task_versions={t.id: task_version(t) for t in plan.tasks},
        capability_versions={i: v for i, s in subjects.items() if (v := s.version()) is not None},
        git_commit=_git("rev-parse", "HEAD"),
        git_dirty=(_git("status", "--porcelain") or "") != "",
        python=sys.version.split()[0],
        platform=platform.platform(),
        machine=f"{platform.machine()}, {os.cpu_count()} logical CPUs",
        playwright=_version("playwright"),
        browser=_browser_version(),
        pricing_sha256=_sha256(PRICING_PATH),
    )
    append_session(info, plan.reports_dir)
    rows = 0
    answered: set[str] = set()
    notes: list[str] = []
    with mockapp() as base_url:
        env = bench_env(base_url, runs_dir)
        for task in plan.tasks:
            subject = subjects[task.id]
            # A task's own environment (a credential the app will reject) is
            # for its runs only.
            task_env = (
                replace(env, environ={**env.environ, **task.environment})
                if task.environment
                else env
            )
            for strategy in plan.strategies:
                if strategy not in task.strategies:
                    continue
                try:
                    for row in _runs(plan, task, subject, task_env, strategy, make_llm, session_id):
                        append_run(row, plan.reports_dir)
                        rows += 1
                        # A run the provider refused outright names the model
                        # it asked for, not one that answered.
                        if row.strategy != "inter_cua_replay" and row.model and row.llm_calls:
                            answered.update(row.model.split(", "))
                        if progress is not None:
                            progress(row)
                except SkipStrategy as exc:
                    notes.append(f"skipped {strategy} for {task.id}: {exc}")
    done = info.model_copy(
        update={
            "finished_at": utc_now(),
            "runs": rows,
            "notes": notes,
            "models_answered": sorted(answered),
        }
    )
    append_session(done, plan.reports_dir)
    return done


def _runs(
    plan: Plan,
    task: BenchmarkTask,
    subject: Subject,
    env: BenchEnv,
    strategy: Strategy,
    make_llm: Callable[[BenchmarkTask], LLMClient],
    session_id: str,
) -> list[RunMetrics]:
    count = plan.count_for(task, strategy)
    out: list[RunMetrics] = []
    group_commits = 0
    cap = subject.capability
    shared = task.shared_idempotency_key
    token = token_for(cap, task, env) if cap is not None and shared else None
    key = f"{session_id}-{task.id}" if shared else None
    for rep in range(1, count + 1):
        earlier = group_commits if shared else 0
        if strategy == "inter_cua_replay" and subject.plan is not None:
            row = run_workflow_replay(
                task,
                subject,
                env,
                session_id=session_id,
                repetition=rep,
                # A workflow that commits runs only under a key.
                idempotency_key=key or f"{session_id}-{task.id}-{rep}",
                commits_earlier=earlier,
            )
        elif strategy == "inter_cua_replay":
            assert cap is not None and subject.path is not None
            row = run_replay(
                task,
                cap,
                subject.path,
                env,
                session_id=session_id,
                repetition=rep,
                token=token or token_for(cap, task, env),
                idempotency_key=key,
                commits_earlier=earlier,
            )
        else:
            row = run_baseline(
                task,
                subject,
                env,
                make_llm,
                session_id=session_id,
                repetition=rep,
                prices=plan.prices,
                commits_earlier=earlier,
                strategy=strategy,
            )
            if strategy == "inter_cua_discovery":
                row = _record_capability(row, env)
        row = row.model_copy(
            update={
                "evidence_bytes": evidence_bytes(row),
                **human_fields(row.run_dir, cached=row.cached),
            }
        )
        group_commits += row.commits_observed or 0
        out.append(row)
    return out


def _provider(plan: Plan) -> tuple[str, str | None]:
    """The provider and model the baseline and discovery will ask: an
    ``auto`` choice and a default model resolved, so the session records
    what was asked rather than how it was spelled."""
    if plan.llm == "scripted":
        return "scripted", None
    client = select_client(plan.llm, plan.model)
    return client.provider, client.model


def _record_capability(row: RunMetrics, env: BenchEnv) -> RunMetrics:
    """Discovery's product is a draft capability; record it beside the run.
    It is never approved here: approval is a person's step."""
    if row.run_dir is None or not row.outcome.startswith("done"):
        return row
    out = env.runs_dir / "capabilities" / f"{row.task_id}-{row.repetition}.json"
    try:
        save(record(Path(row.run_dir), policy=env.policy), out)
    except (RecordError, OSError, ValueError) as exc:
        return row.model_copy(update={"detail": f"{row.detail}; not recordable: {exc}"})
    note = f"recorded draft {out.as_posix()}"
    return row.model_copy(update={"detail": f"{row.detail}; {note}" if row.detail else note})


def _browser_version() -> str | None:
    from cua.surface.playwright_surface import PlaywrightSurface

    try:
        with PlaywrightSurface.launch() as surface:
            owner = surface.page.context.browser
            return owner.version if owner is not None else None
    except Exception:  # a benchmark without a version is still a benchmark
        return None


def _version(package: str) -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(package)
    except PackageNotFoundError:
        return None


def _git(*args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip()


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
