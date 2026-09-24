"""A benchmark session end to end, with the scripted model: no key, no cost.

It starts its own mock app, runs the baseline and replay on real browsers,
and writes raw rows and a report. The numbers that matter here are the ones
the harness must get right whatever the model: that replay makes no model
call, and that a retried commit is counted as a duplicate when it is one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.benchmark.registry import load_suite, select
from cua.benchmark.report import build, write
from cua.benchmark.runner import Plan, run_session
from cua.benchmark.storage import read_runs, read_sessions
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.browser


def test_a_scripted_session_scores_both_strategies_and_catches_a_duplicate_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    suite = load_suite("core")
    tasks = select(
        suite, task_ids=["lookup-success", "lookup-unknown-member", "open-retried-request"]
    )
    reports = tmp_path / "reports"
    info = run_session(
        Plan(
            tasks=tasks,
            strategies=["baseline_llm", "inter_cua_discovery", "inter_cua_replay"],
            suite=suite.name,
            repetitions=2,
            reports_dir=reports,
            runs_root=tmp_path / "runs",
        )
    )
    rows = read_runs(reports)
    assert info.runs == len(rows) == 13  # 2+1+2, 2+2, 2+2
    by = {(r.task_id, r.strategy, r.repetition): r for r in rows}

    for rep in (1, 2):
        replayed = by[("lookup-success", "inter_cua_replay", rep)]
        assert replayed.match == "exact" and replayed.outputs["savings_balance"] == "1411.21"
        assert replayed.llm_calls == 0 and replayed.estimated_cost_usd == 0
        assert by[("lookup-success", "baseline_llm", rep)].match == "exact"

    assert by[("lookup-unknown-member", "inter_cua_replay", 1)].outcome == (
        "business_outcome:NOT_FOUND"
    )
    # The scripted "model" cannot see the answer and stops: a safe stop, not a guess.
    assert by[("lookup-unknown-member", "baseline_llm", 1)].match == "safe_stop"

    discovered = by[("lookup-success", "inter_cua_discovery", 1)]
    assert "recorded draft" in discovered.detail

    # The same request twice: the baseline commits twice, replay once.
    first, again = (by[("open-retried-request", "baseline_llm", r)] for r in (1, 2))
    assert first.commits_observed == 1 and first.match == "exact"
    assert again.commits_observed == 1 and again.duplicate_side_effects == 1
    assert again.match == "wrong"
    once, cached = (by[("open-retried-request", "inter_cua_replay", r)] for r in (1, 2))
    assert once.commits_observed == 1 and once.side_effect == "committed"
    assert cached.cached and cached.commits_observed == 0 and cached.match == "exact"

    (session,) = read_sessions(reports)
    assert session.finished_at is not None and session.llm == "scripted"
    summary = build(rows, read_sessions(reports), suite)
    _, md = write(summary, reports)
    text = md.read_text(encoding="utf-8")
    assert "Scripted model." in text
    assert summary["strategies"]["inter_cua_replay"]["llm_calls"] == 0
    assert summary["strategies"]["baseline_llm"]["duplicate_side_effects"] == 1
