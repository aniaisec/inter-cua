"""The benchmark's bookkeeping: tasks, scoring, prices, aggregation, storage.

No browser and no model: every result here is built by hand, so each rule is
checked on exactly the case it is about.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cua.agent.llm import Decision, DecisionRequest, Provider
from cua.agent.loop import DiscoveryOutcome
from cua.benchmark.aggregation import aggregate, percentile, stats, wilson
from cua.benchmark.baseline_runner import TimedClient, score_baseline
from cua.benchmark.metrics import Commits, outputs_match, score_replay
from cua.benchmark.models import BenchmarkTask, RunMetrics, Suite, Truth
from cua.benchmark.registry import SuiteError, load_suite, select
from cua.benchmark.report import markdown
from cua.benchmark.storage import (
    SessionInfo,
    append_run,
    append_session,
    read_runs,
    read_sessions,
)
from cua.observability.cost import ModelPrice, PriceTable, load_prices, normalize_usage
from cua.replay.result import BusinessOutcome, Failure, Success
from tests.conftest import REPO_ROOT


def task(truth: dict[str, Any], **extra: Any) -> BenchmarkTask:
    return BenchmarkTask.model_validate(
        {
            "id": "t",
            "name": "t",
            "capability": "capabilities/member_savings_balance.json",
            "goal": {"goal": "look it up"},
            "inputs": {"member_id": "10003"},
            "truth": truth,
            **extra,
        }
    )


LOOKUP = task({"kind": "answer", "outputs": {"savings_balance": "1411.21"}})
NOT_FOUND = task(
    {"kind": "business_outcome", "code": "NOT_FOUND", "baseline_pattern": "no matching member"}
)
REFUSE = task({"kind": "refusal"})
COMMIT = task({"kind": "answer", "outputs": {"reference_number": "re:^REF-"}, "commits": 1})

CAP = {"capability": "member_savings_balance", "capability_version": 3}


def outcome(kind: str, message: str = "", reason: str | None = None) -> DiscoveryOutcome:
    return DiscoveryOutcome.model_validate(
        {"kind": kind, "reason": reason, "message": message, "run_id": "r", "run_dir": "d"}
    )


# -- the suite ---------------------------------------------------------------


def test_the_committed_core_suite_loads_and_names_real_capabilities() -> None:
    suite = load_suite("core", root=REPO_ROOT / "bench" / "tasks")
    assert len(suite.tasks) >= 15
    for t in suite.tasks:
        assert (REPO_ROOT / t.capability).is_file(), t.capability
        if t.goal.script:
            assert (REPO_ROOT / t.goal.script).is_file(), t.goal.script
    manual = [t.id for t in suite.tasks if not t.automated]
    assert manual == ["handoff-resume"]


def test_select_refuses_an_unknown_task_rather_than_running_nothing() -> None:
    suite = Suite(name="s", tasks=[LOOKUP])
    with pytest.raises(SuiteError, match="no task nope"):
        select(suite, task_ids=["nope"])


def test_select_leaves_out_tasks_that_need_a_person_unless_asked() -> None:
    manual = LOOKUP.model_copy(update={"id": "m", "automated": False, "tags": ["handoff"]})
    suite = Suite(name="s", tasks=[LOOKUP, manual])
    assert [t.id for t in select(suite)] == ["t"]
    assert [t.id for t in select(suite, include_manual=True)] == ["t", "m"]
    assert [t.id for t in select(suite, tags=["handoff"], include_manual=True)] == ["m"]


def test_a_truth_must_say_what_it_expects() -> None:
    with pytest.raises(ValueError, match="names its outputs"):
        Truth(kind="answer")
    with pytest.raises(ValueError, match="names its code"):
        Truth(kind="business_outcome")


def test_duplicate_task_ids_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate task ids: t"):
        Suite(name="s", tasks=[LOOKUP, LOOKUP])


# -- scoring ---------------------------------------------------------------------


def test_outputs_compare_decimals_numerically_and_regexes_by_search() -> None:
    assert outputs_match({"b": "1411.21"}, {"b": "1411.210"}) == []
    assert outputs_match({"b": "1411.21"}, {"b": "$1,411.21"}) == []
    assert outputs_match({"r": "re:^REF-10003-"}, {"r": "REF-10003-0001"}) == []
    assert outputs_match({"b": "1411.21"}, {"b": "1411.22"}) == ["b='1411.22', expected '1411.21'"]
    assert outputs_match({"b": "1"}, {}) == ["b missing"]


def test_replay_success_with_the_right_outputs_is_exact() -> None:
    result = Success(**CAP, outputs={"savings_balance": "1411.21"})
    assert score_replay(LOOKUP, result, Commits(0)) == ("exact", "")


def test_replay_success_with_a_wrong_value_is_wrong() -> None:
    result = Success(**CAP, outputs={"savings_balance": "9.99"})
    match, detail = score_replay(LOOKUP, result, Commits(0))
    assert match == "wrong" and "9.99" in detail


def test_the_right_business_outcome_is_exact_and_another_is_wrong() -> None:
    assert score_replay(NOT_FOUND, BusinessOutcome(**CAP, code="NOT_FOUND"), Commits(0))[0] == (
        "exact"
    )
    assert score_replay(
        NOT_FOUND, BusinessOutcome(**CAP, code="PERMISSION_DENIED"), Commits(0)
    ) == ("wrong", "reported PERMISSION_DENIED; the truth is business_outcome NOT_FOUND")


def test_a_failure_is_a_safe_stop_when_there_was_an_answer_to_give() -> None:
    result = Failure(**CAP, code="LOCATOR_UNRESOLVED")
    assert score_replay(LOOKUP, result, Commits(0)) == (
        "safe_stop",
        "failure:LOCATOR_UNRESOLVED",
    )


def test_stopping_is_exact_when_stopping_is_the_right_thing() -> None:
    result = Failure(**CAP, code="APP_ERROR")
    assert score_replay(REFUSE, result, Commits(0))[0] == "exact"


def test_a_commit_the_app_counted_overrides_what_the_run_claims() -> None:
    """A run that says nothing happened while the app recorded a commit is
    judged on the app's record."""
    result = Failure(**CAP, code="APP_ERROR", side_effect="none")
    match, detail = score_replay(REFUSE, result, Commits(1))
    assert match == "wrong" and "where none was right" in detail


def test_a_second_commit_for_a_retried_request_is_a_duplicate() -> None:
    commits = Commits(observed=1, earlier_in_group=1)
    assert commits.judge(COMMIT) == (
        1,
        0,
        "committed again (1) after the request had committed",
    )
    assert Commits(observed=0, earlier_in_group=1).judge(COMMIT) == (0, 0, "")
    assert Commits(observed=1).judge(COMMIT) == (0, 0, "")
    assert Commits(observed=2).judge(COMMIT)[0] == 1


def test_baseline_done_is_scored_on_its_outputs() -> None:
    wrong = outcome("done")
    assert score_baseline(LOOKUP, wrong, Commits(0))[0] == "wrong"  # no outputs at all


def test_baseline_stop_reason_matching_the_declared_pattern_is_the_business_answer() -> None:
    said = outcome("escalated", "Search says: No matching member for 99999", "STUCK")
    assert score_baseline(NOT_FOUND, said, Commits(0)) == (
        "exact",
        "stop reason matches NOT_FOUND",
    )
    vague = outcome("escalated", "I could not continue", "STUCK")
    assert score_baseline(NOT_FOUND, vague, Commits(0)) == ("safe_stop", "escalated: STUCK")


def test_baseline_claiming_done_where_the_truth_is_refusal_is_wrong() -> None:
    assert score_baseline(REFUSE, outcome("done"), Commits(0))[0] == "wrong"
    assert score_baseline(REFUSE, outcome("stopped", reason="MAX_STEPS"), Commits(0))[0] == (
        "exact"
    )


# -- model usage and prices ---------------------------------------------------


def test_claude_cache_tokens_are_added_to_the_prompt_count() -> None:
    usage = {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 5}
    assert normalize_usage("anthropic", usage)["input"] == 1000
    gemini = {"input_tokens": 1000, "cache_read_input_tokens": 900, "output_tokens": 5}
    assert normalize_usage("gemini", gemini)["input"] == 1000


PRICES = PriceTable(
    providers={
        "gemini": {
            "gemini-3.8-flash": ModelPrice(
                input=Decimal("0.75"),
                output=Decimal("3.75"),
                cache_read=Decimal("0.075"),
                aliases=["gemini-flash-latest"],
            )
        },
        "anthropic": {
            "claude-sonnet-5": ModelPrice(
                input=Decimal("2"),
                output=Decimal("10"),
                cache_read=Decimal("0.2"),
                cache_write=Decimal("2.5"),
            )
        },
    }
)


def test_cost_charges_cached_input_at_the_cache_rate() -> None:
    cost = PRICES.cost(
        "gemini",
        "gemini-3.8-flash",
        input_tokens=1_000_000,
        output_tokens=100_000,
        thinking_tokens=100_000,
        cache_read_tokens=400_000,
    )
    # 600k x 0.75 + 400k x 0.075 + 200k x 3.75, per million
    assert cost == Decimal("1.230000")


def test_cost_prices_claude_cache_writes() -> None:
    cost = PRICES.cost(
        "anthropic",
        "claude-sonnet-5",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=500_000,
        cache_write_tokens=500_000,
    )
    assert cost == Decimal("1.350000")


def test_an_unpriced_model_is_none_never_zero() -> None:
    assert PRICES.cost("gemini", "gemini-9-ultra", input_tokens=10, output_tokens=10) is None
    assert PRICES.price("gemini", "gemini-flash-latest") is not None
    assert PRICES.cost("scripted", "scripted", input_tokens=0, output_tokens=0) == Decimal(0)


def test_the_committed_price_table_loads() -> None:
    table = load_prices(REPO_ROOT / "bench" / "pricing.yaml")
    assert table.price("gemini", "gemini-3.8-flash") is not None
    assert table.price("anthropic", "claude-sonnet-5") is not None


class _Model:
    provider: Provider = "gemini"
    model = "gemini-flash-latest"

    def decide(self, request: DecisionRequest) -> Decision:
        return Decision(
            tool_call=None,
            response_id="x",
            model="gemini-3.8-flash",
            usage={"input_tokens": 10, "output_tokens": 2, "thinking_tokens": 3},
        )


def test_the_timed_client_counts_calls_tokens_and_the_model_that_answered() -> None:
    client = TimedClient(_Model())
    request = DecisionRequest.model_construct(system="", tools=[], transcript=[])
    client.decide(request)
    client.decide(request)
    assert client.calls == 2
    assert client.usage["input"] == 20 and client.usage["thinking"] == 6
    assert client.answered_by == "gemini-3.8-flash"
    assert client.wait_s >= 0


# -- aggregation ------------------------------------------------------------------


def row(strategy: str, match: str, *, rep: int = 1, s: float = 1.0, **extra: Any) -> RunMetrics:
    return RunMetrics.model_validate(
        {
            "session_id": "bench_1",
            "task_id": "t",
            "strategy": strategy,
            "repetition": rep,
            "started_at": "2026-09-23T00:00:00Z",
            "match": match,
            "success": match == "exact",
            "outcome": "x",
            "truth": "answer",
            "duration_s": s,
            **extra,
        }
    )


def test_wilson_interval_is_honest_about_small_samples() -> None:
    low, high = wilson(10, 10) or (0, 0)
    assert 0.70 < low < 0.73 and high == 1.0
    assert wilson(0, 0) is None


def test_percentile_is_nearest_rank() -> None:
    assert percentile([float(i) for i in range(1, 101)], 95) == 95.0
    assert percentile([3.0], 95) == 3.0
    assert percentile([], 95) is None


def test_stats_counts_matches_rates_and_cost() -> None:
    rows = [
        row("baseline_llm", "exact", rep=1, s=10, llm_calls=6, estimated_cost_usd=Decimal("0.03")),
        row("baseline_llm", "wrong", rep=2, s=20, llm_calls=8, estimated_cost_usd=Decimal("0.05")),
    ]
    got = stats(rows)
    assert got["runs"] == 2 and got["exact"] == 1 and got["wrong"] == 1
    assert got["success_rate"] == 0.5
    assert got["llm_calls_per_run"] == 7
    assert got["estimated_cost_usd"] == "0.08"
    assert got["cost_per_success_usd"] == "0.080000"
    assert got["success_rate_after"] == {"1": 1.0}


def test_one_unpriced_run_makes_the_total_unpriced() -> None:
    rows = [
        row("baseline_llm", "exact", estimated_cost_usd=Decimal("0.03")),
        row("baseline_llm", "exact"),
    ]
    assert stats(rows)["estimated_cost_usd"] is None


def test_break_even_is_discovery_over_the_saving_per_run() -> None:
    rows = [
        row("baseline_llm", "exact", estimated_cost_usd=Decimal("0.03")),
        row("inter_cua_discovery", "exact", estimated_cost_usd=Decimal("0.10")),
        row("inter_cua_replay", "exact", estimated_cost_usd=Decimal("0")),
    ]
    summary = aggregate(rows, {"t": ["success"]})
    assert summary["break_even"]["invocations"] == 4
    scripted = [r.model_copy(update={"provider": "scripted"}) for r in rows]
    assert aggregate(scripted, {})["break_even"] is None
    assert [t["tag"] for t in summary["tags"]] == ["success"] * 3


def test_a_scripted_session_is_flagged_in_the_report() -> None:
    summary = aggregate([row("inter_cua_replay", "exact")], {})
    summary["session_info"] = []
    summary["scripted"] = True
    text = markdown(summary)
    assert "Scripted model." in text
    assert "| inter-cua replay | 1 | 100.0%" in text


# -- storage -----------------------------------------------------------------------


def test_runs_and_sessions_round_trip_and_the_last_session_record_wins(tmp_path: Path) -> None:
    info = SessionInfo(
        session_id="bench_1",
        started_at="a",
        suite="core",
        tasks=["t"],
        strategies=["inter_cua_replay"],
        llm="scripted",
        python="3",
        platform="p",
    )
    append_session(info, tmp_path)
    append_session(info.model_copy(update={"runs": 1, "finished_at": "b"}), tmp_path)
    append_run(row("inter_cua_replay", "exact", estimated_cost_usd=Decimal("0")), tmp_path)
    append_run(row("inter_cua_replay", "exact").model_copy(update={"session_id": "x"}), tmp_path)
    assert [s.runs for s in read_sessions(tmp_path)] == [1]
    assert len(read_runs(tmp_path)) == 2
    assert [r.session_id for r in read_runs(tmp_path, ["bench_1"])] == ["bench_1"]
    assert read_runs(tmp_path)[0].estimated_cost_usd == Decimal("0")


# -- purity ------------------------------------------------------------------------


def test_the_replay_strategy_cannot_reach_a_model() -> None:
    """The benchmark's replay side is held to replay's own rule: nothing it
    imports can load a model client or the agent. The CLI parser, which every
    `cua replay` builds, is held to it too, and so is observability, which
    explains replays."""
    code = (
        "import sys\n"
        "import cua.benchmark.replay_runner, cua.benchmark.metrics, cua.benchmark.report\n"
        "import cua.observability.cli, cua.observability.health\n"
        "import cua.cli; cua.cli._build_parser()\n"
        "bad = [m for m in sys.modules if m == 'anthropic' or m.startswith(('anthropic.',"
        " 'google.genai', 'cua.agent'))]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=REPO_ROOT
    )
    assert out.stdout.strip() == ""
