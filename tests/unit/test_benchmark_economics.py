"""The cost break-even: discovery once plus replay, against the model every time.

No browser and no model: rows are built by hand, so each rule is checked on
the case it is about.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cua.benchmark.economics import analyse, evidence_bytes, markdown, part_costs
from cua.benchmark.models import RunMetrics, Suite
from cua.benchmark.storage import append_run
from cua.cli import main
from cua.observability.cost import InfrastructurePrice, PriceTable, load_prices
from tests.conftest import REPO_ROOT

INFRA = InfrastructurePrice(
    browser_hour=Decimal("3.6"),  # a cent per second, so the numbers read off
    storage_gb_month=Decimal("1"),
    retention_months=Decimal("1"),
)
GIB = 1024**3


def row(strategy: str, task: str = "a", rep: int = 1, **extra: Any) -> RunMetrics:
    fields: dict[str, Any] = {
        "session_id": "bench_1",
        "task_id": task,
        "strategy": strategy,
        "repetition": rep,
        "started_at": "2026-09-29T00:00:00Z",
        "match": "exact",
        "success": True,
        "outcome": "success",
        "truth": "answer",
        "duration_s": 1.0,
        "evidence_bytes": 0,
        "estimated_cost_usd": Decimal(0),
    }
    if strategy != "inter_cua_replay":
        fields |= {"provider": "gemini", "model": "gemini-3.8-flash", "llm_calls": 8}
    return RunMetrics.model_validate(fields | extra)


def suite(*tasks: tuple[str, str]) -> Suite:
    return Suite.model_validate(
        {
            "name": "t",
            "tasks": [
                {
                    "id": tid,
                    "name": tid,
                    "capability": f"capabilities/{cap}.json",
                    "goal": {"goal": "g"},
                    "inputs": {},
                    "truth": {"kind": "refusal"},
                }
                for tid, cap in tasks
            ],
        }
    )


ONE = suite(("a", "cap"))


def rows(
    base: str = "0.05", disc: str = "0.05", rep: str = "0", n: int = 10, **extra: Any
) -> list[RunMetrics]:
    out = [row("inter_cua_discovery", estimated_cost_usd=Decimal(disc), **extra)]
    out += [row("baseline_llm", rep=i, estimated_cost_usd=Decimal(base), **extra) for i in range(n)]
    out += [
        row("inter_cua_replay", rep=i, estimated_cost_usd=Decimal(rep), **extra) for i in range(n)
    ]
    return out


def only(result: dict[str, Any]) -> dict[str, Any]:
    (cap,) = result["capabilities"]
    return cap


def test_break_even_is_discovery_over_the_saving_per_run() -> None:
    cap = only(analyse(rows(base="0.02", disc="0.05"), ONE, PriceTable()))
    assert cap["priced_parts"] == ["model"]
    assert cap["break_even"]["invocations"] == 3  # 0.05 / 0.02 = 2.5
    assert cap["break_even"]["exact"] == 2.5
    assert cap["break_even"]["invocations_ci95"] == [3, 3]  # every run costs the same


def test_volumes_add_discovery_once_and_replay_every_time() -> None:
    cap = only(analyse(rows(base="0.02", disc="0.05", rep="0.001"), ONE, PriceTable()))
    ten = cap["at_volume"][0]
    assert ten["invocations"] == 10
    assert Decimal(ten["traditional_usd"]) == Decimal("0.2")
    assert Decimal(ten["inter_cua_usd"]) == Decimal("0.06")
    assert Decimal(ten["saved_usd"]) == Decimal("0.14")
    assert [v["invocations"] for v in cap["at_volume"]] == [10, 100, 1000, 10000]


def test_replay_pays_for_its_browser_and_its_evidence() -> None:
    data = rows(duration_s=10.0, evidence_bytes=GIB // 100)
    cap = only(analyse(data, ONE, PriceTable(infrastructure=INFRA)))
    assert cap["priced_parts"] == ["model", "browser", "storage"]
    replay = cap["replay"]["per_run_usd"]
    assert Decimal(replay["browser"]) == Decimal("0.01")
    assert Decimal(replay["storage"]) == Decimal("0.01")
    assert Decimal(cap["replay_per_run_usd"]) == Decimal("0.02")
    # The baseline pays the same browser and storage on top of its model.
    assert Decimal(cap["baseline_per_run_usd"]) == Decimal("0.07")


def test_replay_that_costs_as_much_as_the_model_never_breaks_even() -> None:
    cap = only(analyse(rows(base="0.01", rep="0.01"), ONE, PriceTable()))
    assert cap["break_even"]["invocations"] is None
    assert "never" in markdown(analyse(rows(base="0.01", rep="0.01"), ONE, PriceTable()))


def test_a_failed_discovery_is_part_of_what_the_capability_cost() -> None:
    data = rows(disc="0.04")
    data.append(
        row(
            "inter_cua_discovery",
            rep=2,
            match="safe_stop",
            success=False,
            outcome="escalated:STUCK",
            estimated_cost_usd=Decimal("0.04"),
        )
    )
    cap = only(analyse(data, ONE, PriceTable()))
    assert cap["discovery"]["obtained"] == 1
    assert Decimal(cap["discovery_usd"]) == Decimal("0.08")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"success": False, "match": "safe_stop"}, "no discovery run produced"),
        ({"provider": "scripted"}, "a script, not a model"),
        ({"estimated_cost_usd": None}, "unpriced"),
    ],
)
def test_no_break_even_is_claimed_without_the_numbers_for_one(
    change: dict[str, Any], reason: str
) -> None:
    data = [
        r if r.strategy != "inter_cua_discovery" else r.model_copy(update=change) for r in rows()
    ]
    cap = only(analyse(data, ONE, PriceTable()))
    assert "break_even" not in cap
    assert reason in cap["reason"]
    assert reason in markdown(analyse(data, ONE, PriceTable()))


def test_only_tasks_both_strategies_ran_set_the_price() -> None:
    both = suite(("a", "cap"), ("b", "cap"))
    data = [*rows(base="0.02"), row("baseline_llm", task="b", estimated_cost_usd=Decimal("9"))]
    cap = only(analyse(data, both, PriceTable()))
    assert cap["tasks"] == ["a"] and cap["left_out_tasks"] == ["b"]
    assert Decimal(cap["baseline_per_run_usd"]) == Decimal("0.02")


def test_each_capability_breaks_even_on_its_own_tasks() -> None:
    two = suite(("a", "cheap"), ("b", "dear"))
    data = rows(base="0.01", disc="0.05") + [
        r.model_copy(
            update={"task_id": "b", "estimated_cost_usd": (r.estimated_cost_usd or 0) * 10}
        )
        for r in rows(base="0.01", disc="0.05")
    ]
    result = analyse(data, two, PriceTable())
    by = {c["capability"]: c["break_even"]["invocations"] for c in result["capabilities"]}
    assert by == {"cheap": 5, "dear": 5}


def test_a_task_without_a_capability_is_named_and_left_out() -> None:
    data = [*rows(), row("baseline_llm", task="upload-attempt")]
    result = analyse(data, ONE, PriceTable())
    assert any("upload-attempt" in n for n in result["notes"])


def test_runs_lost_to_the_provider_are_not_priced() -> None:
    lost = row("baseline_llm", error_code="LLM_ERROR", llm_calls=0, estimated_cost_usd=Decimal(0))
    cap = only(analyse(rows(base="0.02", n=4) + [lost] * 4, ONE, PriceTable()))
    assert cap["baseline"]["runs"] == 4
    assert Decimal(cap["baseline_per_run_usd"]) == Decimal("0.02")


def test_evidence_is_measured_from_the_run_directory(tmp_path: Path) -> None:
    run = tmp_path / "run_1"
    (run / "screenshots").mkdir(parents=True)
    (run / "log.jsonl").write_bytes(b"x" * 100)
    (run / "screenshots" / "0001.png").write_bytes(b"x" * 900)
    assert evidence_bytes(row("inter_cua_replay", run_dir=str(run))) == 1000
    # An answer from the idempotency store points at the original run's directory.
    assert evidence_bytes(row("inter_cua_replay", run_dir=str(run), cached=True)) == 0
    assert evidence_bytes(row("inter_cua_replay", run_dir=str(tmp_path / "gone"))) is None


def test_a_run_whose_evidence_is_gone_is_left_out_of_the_storage_mean(tmp_path: Path) -> None:
    kept = rows(evidence_bytes=GIB // 100)
    gone = [
        r.model_copy(update={"evidence_bytes": None, "run_dir": str(tmp_path / "gone")})
        for r in rows()
        if r.strategy == "inter_cua_replay"
    ][:5]
    cap = only(analyse(kept + gone, ONE, PriceTable(infrastructure=INFRA)))
    assert cap["replay"]["evidence_measured_runs"] == 10 and cap["replay"]["runs"] == 15
    assert Decimal(cap["replay"]["per_run_usd"]["storage"]) == Decimal("0.01")
    assert "10 of 15 measured" in markdown(analyse(kept + gone, ONE, PriceTable()))


def test_without_infrastructure_prices_the_report_says_model_only() -> None:
    result = analyse(rows(), ONE, PriceTable())
    costs = part_costs(rows()[0], None, 1000)
    assert costs["browser"] is None and costs["storage"] is None
    assert "Browser and storage are not priced" in markdown(result)


def test_the_committed_price_table_prices_infrastructure() -> None:
    prices = load_prices(REPO_ROOT / "bench" / "pricing.yaml")
    assert prices.infrastructure is not None
    assert prices.infrastructure.browser_hour > 0 and prices.infrastructure.retention_months > 0
    assert prices.infrastructure.source


def test_cua_benchmark_break_even_writes_both_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for r in rows(base="0.02", disc="0.05"):
        append_run(r, tmp_path)
    out = tmp_path / "out"
    suite_file = tmp_path / "suite.yaml"
    suite_file.write_text(json.dumps(ONE.model_dump(mode="json", exclude_none=True)))
    code = main(
        [
            "benchmark",
            "break-even",
            "--suite",
            str(suite_file),
            "--reports-dir",
            str(tmp_path),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    data = json.loads((out / "break_even.json").read_text(encoding="utf-8"))
    assert data["capabilities"][0]["break_even"]["invocations"] == 3
    assert "# Cost break-even" in (out / "break_even.md").read_text(encoding="utf-8")
    assert "**3**" in capsys.readouterr().out
