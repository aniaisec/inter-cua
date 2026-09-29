"""The statistics behind a benchmark comparison, and what a session records.

No browser and no model: rows are built by hand, so each rule is checked on
the case it is about.
"""

from __future__ import annotations

import statistics
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cua.agent.llm import generation_settings
from cua.agent.prompts import prompt_version
from cua.benchmark.environment import app_version
from cua.benchmark.inference import (
    adequacy,
    bootstrap_ci,
    bootstrap_diff_ci,
    compare,
    consistency,
    modal_outcomes,
    newcombe,
)
from cua.benchmark.models import BenchmarkTask, RunMetrics
from cua.benchmark.registry import load_suite
from cua.benchmark.report import build, markdown
from cua.benchmark.runner import Plan
from cua.benchmark.storage import SessionInfo
from cua.benchmark.subject import subject_for, task_version
from tests.conftest import REPO_ROOT


def row(
    strategy: str,
    match: str = "exact",
    *,
    task: str = "t",
    rep: int = 1,
    s: float = 1.0,
    **extra: Any,
) -> RunMetrics:
    return RunMetrics.model_validate(
        {
            "session_id": "bench_1",
            "task_id": task,
            "strategy": strategy,
            "repetition": rep,
            "started_at": "2026-09-28T00:00:00Z",
            "match": match,
            "success": match == "exact",
            "outcome": "success" if match == "exact" else "failure:APP_ERROR",
            "truth": "answer",
            "duration_s": s,
            **extra,
        }
    )


def live(n_base: int, n_rep: int, tasks: tuple[str, ...] = ("a", "b")) -> list[RunMetrics]:
    """Baseline runs from a priced live model and replay runs, per task."""
    rows = []
    for t in tasks:
        rows += [
            row(
                "baseline_llm",
                task=t,
                rep=i,
                s=20.0 + i % 7,
                llm_calls=8,
                provider="gemini",
                model="gemini-3.8-flash",
                estimated_cost_usd=Decimal("0.05"),
            )
            for i in range(1, n_base + 1)
        ]
        rows += [
            row("inter_cua_replay", task=t, rep=i, s=10.0 + i % 3, estimated_cost_usd=Decimal(0))
            for i in range(1, n_rep + 1)
        ]
    return rows


# -- intervals --------------------------------------------------------------------


def test_newcombe_matches_the_published_example() -> None:
    # Newcombe (1998), example (a): 56/70 - 48/80, method 10.
    assert newcombe(56, 70, 48, 80) == (0.0524, 0.3339)
    assert newcombe(1, 0, 1, 1) is None


def test_a_difference_of_two_empty_rates_still_has_width() -> None:
    lo, hi = newcombe(0, 10, 0, 20) or (0, 0)
    assert lo < 0 < hi


def test_the_bootstrap_is_reproducible_and_brackets_the_estimate() -> None:
    values = [float(v) for v in range(1, 31)]
    ci = bootstrap_ci(values)
    assert ci == bootstrap_ci(values)
    assert ci is not None and ci[0] < statistics.median(values) < ci[1]
    assert bootstrap_ci([]) is None


def test_a_bootstrap_difference_excludes_zero_only_when_the_samples_separate() -> None:
    slow = [20.0 + v % 5 for v in range(30)]
    fast = [10.0 + v % 5 for v in range(30)]
    lo, hi = bootstrap_diff_ci(fast, slow) or (0, 0)
    assert hi < 0
    lo, hi = bootstrap_diff_ci(slow, list(reversed(slow))) or (1, 1)
    assert lo <= 0 <= hi


# -- repeatability ----------------------------------------------------------------


def test_the_modal_outcome_is_what_a_task_usually_ends_with() -> None:
    rows = [row("baseline_llm"), row("baseline_llm"), row("baseline_llm", "safe_stop")]
    assert modal_outcomes(rows) == {("baseline_llm", "t"): "success"}


def test_consistency_counts_tasks_that_always_end_the_same_way() -> None:
    rows = [
        row("inter_cua_replay", task="a", s=10.0),
        row("inter_cua_replay", task="a", s=10.0),
        row("inter_cua_replay", task="b"),
        row("inter_cua_replay", "safe_stop", task="b"),
        row("inter_cua_replay", task="c"),  # one run: nothing to repeat
    ]
    got = consistency(rows)
    assert got["tasks"] == 2 and got["same_outcome_every_time"] == 1
    assert got["same_outcome_share"] == 0.5


# -- the comparison ---------------------------------------------------------------


def test_protocol_adequacy_is_judged_per_task() -> None:
    fit = adequacy(live(30, 10))
    assert fit["baseline_llm"]["met"] and fit["inter_cua_replay"]["met"]
    short = adequacy(live(30, 10)[:-1])  # one replay run short on one task
    assert short["inter_cua_replay"]["min_runs_per_task"] == 9
    assert not short["inter_cua_replay"]["met"]


def test_a_live_comparison_that_meets_the_protocol_is_supported() -> None:
    got = compare(live(30, 10), {})
    assert got["strength"] == "supported" and got["live_model"]
    by = {q["id"]: q for q in got["questions"]}
    assert len(by) == 7
    assert by["llm_calls"]["verdict"] == "replay lower"
    assert by["cost"]["verdict"] == "replay lower"
    assert by["latency"]["verdict"] == "replay lower"
    # Everything succeeded both ways: nothing to tell apart.
    assert by["repeatability"]["verdict"] == "no detectable difference"
    assert by["safety"]["verdict"] == "no detectable difference"


def test_below_the_protocol_the_same_numbers_are_indicative() -> None:
    got = compare(live(3, 3), {})
    assert got["strength"].startswith("indicative")
    assert {q["id"]: q["verdict"] for q in got["questions"]}["llm_calls"] == "replay lower"


def test_nothing_is_compared_against_a_scripted_baseline() -> None:
    rows = [r.model_copy(update={"provider": "scripted"}) for r in live(30, 10)]
    got = compare(rows, {})
    assert got["strength"].startswith("not measured")
    assert {q["verdict"] for q in got["questions"]} == {"not measured"}


def test_only_tasks_both_strategies_ran_are_compared() -> None:
    rows = [*live(30, 10), row("inter_cua_replay", task="replay-only", s=99.0)]
    got = compare(rows, {})
    assert got["tasks"] == ["a", "b"] and got["excluded_tasks"] == ["replay-only"]
    latency = {q["id"]: q for q in got["questions"]}["latency"]
    assert latency["replay"]["n"] == 20


def test_an_unpriced_run_leaves_cost_unmeasured() -> None:
    rows = live(30, 10)
    rows[0] = rows[0].model_copy(update={"estimated_cost_usd": None})
    cost = {q["id"]: q for q in compare(rows, {})["questions"]}["cost"]
    assert cost["verdict"] == "not measured" and cost["baseline"]["n"] == 0


def test_drift_is_read_from_the_category_or_the_tag() -> None:
    rows = live(30, 10)
    rows = [
        r.model_copy(update={"category": "drift"})
        if r.task_id == "a" and r.strategy == "inter_cua_replay"
        else r
        for r in rows
    ]
    drift = {q["id"]: q for q in compare(rows, {"a": ["drift"]})["questions"]}["drift"]
    assert drift["baseline"]["n"] == 30 and drift["replay"]["n"] == 10


def test_a_duplicate_commit_is_an_unsafe_run() -> None:
    rows = live(30, 10)
    rows[0] = rows[0].model_copy(update={"match": "wrong", "duplicate_side_effects": 1})
    safety = {q["id"]: q for q in compare(rows, {})["questions"]}["safety"]
    assert safety["baseline"]["count"] == 1 and safety["replay"]["count"] == 0
    assert safety["baseline"]["duplicate_commits"] == 1


def test_the_report_states_the_questions_and_their_strength() -> None:
    rows = live(3, 3)
    summary: dict[str, Any] = {
        "strategies": {},
        "tasks": [],
        "tags": [],
        "session_info": [],
        "comparison": compare(rows, {}),
    }
    text = markdown(summary)
    assert "## What the measurements support" in text
    assert "Does deterministic replay reduce LLM calls?" in text
    assert "indicative: fewer runs per task than the protocol asks" in text
    assert "3 runs per task (protocol 30+)" in text


def test_the_report_warns_when_sessions_ran_different_capabilities() -> None:
    base = SessionInfo(
        session_id="bench_1",
        started_at="a",
        suite="core",
        tasks=["t"],
        strategies=["inter_cua_replay"],
        llm="scripted",
        python="3",
        platform="p",
        capability_versions={"t": "cap v1 sha256:aaa"},
    )
    other = base.model_copy(
        update={"session_id": "bench_2", "capability_versions": {"t": "cap v2 sha256:bbb"}}
    )
    summary: dict[str, Any] = {
        "strategies": {},
        "tasks": [],
        "tags": [],
        "session_info": [base.model_dump(mode="json"), other.model_dump(mode="json")],
    }
    assert "the capability changed between sessions for t" in markdown(summary)


# -- what a session records ---------------------------------------------------------


def test_an_old_session_record_still_loads() -> None:
    old = (
        '{"session_id": "bench_0", "started_at": "a", "suite": "core", "tasks": [], '
        '"strategies": [], "llm": "gemini", "python": "3", "platform": "p"}'
    )
    info = SessionInfo.model_validate_json(old)
    assert info.task_versions == {} and info.prompt_version is None


def test_versions_are_stable_and_move_when_the_thing_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    task = next(t for t in load_suite("core").tasks if t.id == "lookup-success")
    assert task_version(task) == task_version(task)
    edited = BenchmarkTask.model_validate({**task.model_dump(), "inputs": {"member_id": "1"}})
    assert task_version(edited) != task_version(task)
    assert prompt_version().startswith("sha256:") and prompt_version() == prompt_version()
    assert (app_version() or "").startswith("sha256:")
    assert app_version(Path("no-such-dir")) is None


def test_a_capability_and_a_workflow_record_what_replay_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    tasks = {t.id: t for t in load_suite("all").tasks}
    lookup = next(t for t in load_suite("core").tasks if t.id == "lookup-success")
    cap = subject_for(lookup).version() or ""
    assert cap.startswith("member_savings_balance v") and "sha256:" in cap
    workflow = next(t for t in tasks.values() if t.workflow)
    got = subject_for(workflow).version() or ""
    assert got.startswith("open_member_subaccount sha256:")
    assert "member_savings_balance v" in got and "open_subaccount v" in got
    alone = next(t for t in tasks.values() if not t.capability and not t.workflow)
    assert subject_for(alone).version() is None


def test_settings_say_the_temperature_is_left_to_the_provider() -> None:
    for provider in ("anthropic", "gemini"):
        assert generation_settings(provider)["temperature"] == "provider default"
    assert "sampling" in generation_settings("scripted")


def test_a_strategy_override_beats_the_session_and_the_task() -> None:
    task = next(t for t in load_suite(str(REPO_ROOT / "bench/tasks/core.yaml")).tasks)
    plan = Plan(
        tasks=[task],
        strategies=["baseline_llm", "inter_cua_replay", "inter_cua_discovery"],
        suite="core",
        repetitions=5,
        repetitions_by_strategy={"baseline_llm": 30},
    )
    assert plan.count_for(task, "baseline_llm") == 30
    assert plan.count_for(task, "inter_cua_replay") == 5
    assert plan.count_for(task, "inter_cua_discovery") == 1
    assert Plan(tasks=[task], strategies=[], suite="core").count_for(task, "inter_cua_replay") == (
        task.repetitions
    )


def test_a_run_the_provider_refused_is_set_aside_not_scored() -> None:
    measured = live(30, 10)
    refused = [
        row("baseline_llm", "safe_stop", task="a", rep=100 + i, error_code="LLM_ERROR")
        for i in range(3)
    ]
    # The model answered, then failed: the baseline's own reliability, kept.
    failed_late = row("baseline_llm", "safe_stop", task="b", error_code="LLM_ERROR", llm_calls=4)
    summary = build([*measured, *refused, failed_late], [], None)
    assert summary["runs"] == len(measured) + 1
    assert summary["lost_runs"] == [
        {"session": "bench_1", "task": "a", "strategy": "baseline_llm", "runs": 3}
    ]
    assert "Runs lost to the provider" in markdown(summary)
