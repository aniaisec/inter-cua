"""Evaluate a candidate before anyone is asked to approve it: the benchmark
tasks for its capability, replayed with the candidate and with the version
it repairs, side by side, in a fresh mock app.

It goes through the benchmark's own replay path (``cua.benchmark
.replay_runner``): the same approval gate, signed consent minted for this
session only, idempotency keys, a fresh browser per run, no model anywhere.
The candidate is a draft, so it runs with ``allow_draft``; nothing else is
relaxed.

**Gates** (all must pass):

``checks``            no static check failed (``cua.drift.checks``).
``tasks_ran``         at least one benchmark task exercises the capability.
``repairs_the_drift`` every task that injects the fault the drift was seen
                      under is answered exactly by the candidate. A drift
                      that was not injected has no such task; the gate says
                      so and rests on the failure screen alone.
``no_regression``     on every other task the candidate is exact at least as
                      often as the version it repairs.
``no_wrong_answer``   the candidate never gave a wrong answer or made a
                      commit it should not have.
``security_tasks``    tasks tagged ``security`` (a commit without consent,
                      say) are answered exactly: the candidate refuses what
                      the version it repairs refuses.

Passing does not approve anything. It records that this content passed, so
that ``cua approve`` will accept it once a person has read it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cua.artifact.schema import Capability
from cua.artifact.store import ArtifactError, load
from cua.benchmark.environment import bench_env, mockapp
from cua.benchmark.models import BenchmarkTask, RunMetrics
from cua.benchmark.registry import load_suite, select
from cua.benchmark.replay_runner import consent, run_replay
from cua.drift.models import Evaluation, Gate, SideResult, TaskResult
from cua.drift.store import Candidate, CandidateError, Candidates
from cua.evidence.logger import new_run_id

RUNS_ROOT = Path("bench/runs/candidates")
Progress = Callable[[str, RunMetrics], None]
"""(``incumbent`` | ``candidate``, the run's metrics)."""


@dataclass(frozen=True)
class _Side:
    label: str
    capability: Capability
    path: Path
    allow_draft: bool


def tasks_for(name: str, suite: str = "core", task_ids: Iterable[str] = ()) -> list[BenchmarkTask]:
    """The suite's automated tasks that run this capability."""
    chosen = select(load_suite(suite), task_ids=task_ids)
    out = []
    for task in chosen:
        try:
            if load(Path(task.capability)).name == name:
                out.append(task)
        except ArtifactError:
            continue
    return out


def evaluate(
    candidate: Candidate,
    *,
    capabilities_dir: Path,
    suite: str = "core",
    task_ids: Iterable[str] = (),
    repetitions: int = 1,
    runs_root: Path = RUNS_ROOT,
    progress: Progress | None = None,
) -> Candidate:
    if candidate.edited_outside:
        raise CandidateError(
            f"{candidate.path.as_posix()} was changed after it was proposed; propose it again"
        )
    task_ids = list(task_ids)
    r = candidate.record
    try:
        incumbent = load(Path(r.base.path))
    except ArtifactError as exc:
        raise CandidateError(f"the version it repairs cannot be loaded: {exc}") from None
    if incumbent.content_hash() != r.base.artifact_hash:
        raise CandidateError(f"{r.base.path} is no longer {r.name} v{r.base.version} as it drifted")
    tasks = tasks_for(r.name, suite, task_ids)
    sides = (
        _Side("incumbent", incumbent, Path(r.base.path), allow_draft=False),
        _Side("candidate", candidate.capability, candidate.path, allow_draft=True),
    )
    eval_id = "eval_" + new_run_id().removeprefix("run_")
    runs_dir = runs_root / f"{r.name}-v{r.version}" / eval_id
    results: list[TaskResult] = []
    injected = r.drift[0].injected
    if tasks:
        with mockapp() as base_url:
            env = bench_env(base_url, runs_dir)
            for task in tasks:
                got = {}
                for side in sides:
                    rows = []
                    group = 0
                    shared = task.consent and task.shared_idempotency_key
                    token = consent(side.capability, task, env) if shared else None
                    key = (
                        f"{eval_id}-{side.label}-{task.id}" if task.shared_idempotency_key else None
                    )
                    for rep in range(1, repetitions + 1):
                        row = run_replay(
                            task,
                            side.capability,
                            side.path,
                            env,
                            session_id=eval_id,
                            repetition=rep,
                            token=token
                            or (consent(side.capability, task, env) if task.consent else None),
                            idempotency_key=key,
                            commits_earlier=group if task.shared_idempotency_key else 0,
                            allow_draft=side.allow_draft,
                        )
                        group += row.commits_observed or 0
                        rows.append(row)
                        if progress is not None:
                            progress(side.label, row)
                    got[side.label] = _side(rows)
                results.append(
                    TaskResult(
                        task_id=task.id,
                        name=task.name,
                        tags=list(task.tags),
                        inject=task.inject,
                        reproduces_drift=injected is not None and task.inject == injected,
                        incumbent=got["incumbent"],
                        candidate=got["candidate"],
                    )
                )
    gates = _gates(candidate, results)
    evaluation = Evaluation(
        at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        artifact_hash=r.artifact_hash,
        suite=suite,
        task_filter=list(task_ids),
        repetitions=repetitions,
        runs_dir=runs_dir.as_posix(),
        tasks=results,
        checks=list(r.checks),
        gates=gates,
        passed=all(g.passed for g in gates),
    )
    store = Candidates(capabilities_dir)
    store.write(candidate.dir, r.model_copy(update={"evaluation": evaluation}))
    return store.read(candidate.dir)


def _side(rows: list[RunMetrics]) -> SideResult:
    return SideResult(
        runs=len(rows),
        exact=sum(1 for x in rows if x.match == "exact"),
        safe_stop=sum(1 for x in rows if x.match == "safe_stop"),
        wrong=sum(1 for x in rows if x.match == "wrong"),
        outcomes=[x.outcome for x in rows],
        run_ids=[x.run_id for x in rows],
    )


def _gates(candidate: Candidate, results: list[TaskResult]) -> list[Gate]:
    r = candidate.record
    failed = [c for c in r.checks if c.result == "fail"]
    gates = [
        Gate(
            id="checks",
            passed=not failed,
            detail="no static check failed"
            if not failed
            else "failed: " + ", ".join(c.id for c in failed),
        ),
        Gate(
            id="tasks_ran",
            passed=bool(results),
            detail=f"{len(results)} benchmark task(s) exercise {r.name}"
            if results
            else f"no benchmark task exercises {r.name}; add one before approving a repair",
        ),
    ]
    trigger = [t for t in results if t.reproduces_drift]
    if trigger:
        missed = [t.task_id for t in trigger if t.candidate.exact < t.candidate.runs]
        gates.append(
            Gate(
                id="repairs_the_drift",
                passed=not missed,
                detail=(
                    f"exact on {', '.join(t.task_id for t in trigger)}, where v{r.base.version} "
                    f"was exact {sum(t.incumbent.exact for t in trigger)}/"
                    f"{sum(t.incumbent.runs for t in trigger)}"
                    if not missed
                    else f"not exact on {', '.join(missed)}"
                ),
            )
        )
    else:
        gates.append(
            Gate(
                id="repairs_the_drift",
                passed=True,
                detail="no benchmark task reproduces this drift (it was not injected); the "
                "repair rests on the recorded failure screen",
            )
        )
    worse = [
        f"{t.task_id} ({t.candidate.exact}/{t.candidate.runs} vs "
        f"{t.incumbent.exact}/{t.incumbent.runs})"
        for t in results
        if not t.reproduces_drift and t.candidate.exact < t.incumbent.exact
    ]
    gates.append(
        Gate(
            id="no_regression",
            passed=not worse,
            detail="exact wherever the version it repairs is exact"
            if not worse
            else "worse on " + ", ".join(worse),
        )
    )
    wrong = [t.task_id for t in results if t.candidate.wrong]
    gates.append(
        Gate(
            id="no_wrong_answer",
            passed=not wrong,
            detail="no wrong answer and no unexpected commit"
            if not wrong
            else "wrong on " + ", ".join(wrong),
        )
    )
    security = [t for t in results if "security" in t.tags]
    unsafe = [t.task_id for t in security if t.candidate.exact < t.candidate.runs]
    gates.append(
        Gate(
            id="security_tasks",
            passed=not unsafe,
            detail=(
                f"exact on {', '.join(t.task_id for t in security)}"
                if security and not unsafe
                else "no security task exercises this capability"
                if not security
                else "not exact on " + ", ".join(unsafe)
            ),
        )
    )
    return gates
