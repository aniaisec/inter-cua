"""Human intervention economics: every request to a person, how it ended, and
what it took.

Read from the committed evidence where it has the case (an approval, a
recovery, a manual completion, a real person's approval during discovery),
and from run directories built here for the rest (an abort, an expiry, a
request nobody took, a handback no checkpoint held on).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cua.benchmark.aggregation import stats
from cua.benchmark.metrics import human_fields
from cua.benchmark.models import RunMetrics as Row
from cua.cli import main
from cua.observability.cost import HumanPrice, load_prices
from cua.observability.humans import markdown, summarise
from cua.observability.metrics import Intervention, RunMetrics, run_metrics
from cua.observability.recorder import read_run
from tests.conftest import REPO_ROOT

EVIDENCE = REPO_ROOT / "evidence"


def _one(run_dir: Path) -> Intervention:
    (only,) = run_metrics(read_run(run_dir)).human.interventions
    return only


# -- the committed evidence ---------------------------------------------------------


def test_an_approval_from_the_console() -> None:
    i = _one(EVIDENCE / "replay-needs-approval")
    assert (i.kind, i.reason, i.decided_by) == ("approval", "NEEDS_APPROVAL", "evidence-bot")
    assert i.resumed_at == "review.submit" and i.browser_actions == 0


def test_a_recovery_is_a_handback_the_automation_carried_on_from() -> None:
    i = _one(EVIDENCE / "replay-escalation")
    assert (i.kind, i.reason, i.resumed_at) == ("recovery", "STUCK", "member.open_sub_account")
    assert i.browser_actions == 1  # the person's click; the console's take-control is not one
    assert i.in_control_s == pytest.approx(1.53, abs=0.01)
    assert i.queued_s == pytest.approx(0.03, abs=0.01)


def test_a_manual_completion_left_only_the_outputs_to_read() -> None:
    i = _one(EVIDENCE / "replay-resume-after-human")
    # Nobody took it while the first process waited; it was answered later
    # and resumed with `cua resume`: the answer is what counts.
    assert (i.kind, i.resumed_at) == ("manual_completion", "done")
    assert i.browser_actions == 2


def test_a_person_approving_during_discovery_is_timed() -> None:
    i = _one(EVIDENCE / "discovery-open-subaccount")
    # The discovery loop names its reason reason_code.
    assert (i.kind, i.reason, i.decided_by) == ("approval", "NEEDS_APPROVAL", "anil")
    assert i.queued_s == pytest.approx(26.3, abs=0.1)
    assert i.human_s == pytest.approx(26.3, abs=0.1)


# -- built runs ------------------------------------------------------------------------

T = "2026-09-29T10:00:{:06.3f}Z"


def _ts(s: float) -> str:
    return T.format(s)


def _run(
    root: Path,
    name: str,
    log: list[dict[str, Any]],
    *,
    transitions: list[tuple[float, str, str | None]] | None = None,
    humans: list[dict[str, Any]] | None = None,
    kind: str = "replay",
) -> Path:
    d = root / name
    d.mkdir(parents=True)
    header: dict[str, Any] = {
        "run_id": name,
        "kind": kind,
        "started_at": _ts(0),
        "tenant": {"id": "t"},
    }
    if kind == "replay":
        header["capability"] = {"id": "cap_X", "name": "cap_x", "version": 1}
    else:
        header["goal"] = {"goal": "g", "name": "cap_x"}
    (d / "run.json").write_text(json.dumps(header), encoding="utf-8")
    lines = [{"ts": _ts(0.1), "event": "run.start"}, *log]
    (d / "log.jsonl").write_text(
        "\n".join(json.dumps({"seq": i, **x}) for i, x in enumerate(lines)) + "\n", encoding="utf-8"
    )
    if transitions:
        (d / "state_transitions.jsonl").write_text(
            "\n".join(
                json.dumps({"ts": _ts(t), "to": to, "request_id": req})
                for t, to, req in transitions
            )
            + "\n",
            encoding="utf-8",
        )
    if humans is not None:
        (d / "human_actions.jsonl").write_text(
            "\n".join(json.dumps(h) for h in humans) + "\n", encoding="utf-8"
        )
    return d


def _ask(at: float, req: str | None, reason: str = "STUCK") -> dict[str, Any]:
    line: dict[str, Any] = {"ts": _ts(at), "event": "escalation.requested", "reason": reason}
    if req is not None:
        line["request"] = req
    return line


def _end(at: float, kind: str) -> dict[str, Any]:
    return {"ts": _ts(at), "event": "run.end", "kind": kind}


def test_a_person_stopping_the_run_and_nobody_answering_are_different(tmp_path: Path) -> None:
    aborted = _run(
        tmp_path,
        "run_ABORT",
        [
            _ask(1, "req_1"),
            {
                "ts": _ts(9),
                "event": "handoff.aborted",
                "request": "req_1",
                "by": "ops",
                "why": "no",
            },
            _end(9.5, "failure"),
        ],
        transitions=[
            (1, "PAUSED", "req_1"),
            (4, "HUMAN_IN_CONTROL", "req_1"),
            (9, "ABORTED", "req_1"),
        ],
    )
    expired = _run(
        tmp_path,
        "run_EXPIRED",
        [
            _ask(1, "req_2"),
            {"ts": _ts(30), "event": "handoff.aborted", "request": "req_2", "by": "timeout"},
            _end(30.5, "failure"),
        ],
        transitions=[(1, "PAUSED", "req_2"), (30, "ABORTED", "req_2")],
    )
    a, e = _one(aborted), _one(expired)
    assert (a.kind, a.decided_by, a.queued_s, a.in_control_s) == ("abort", "ops", 3.0, 5.0)
    assert (e.kind, e.decided_by, e.queued_s, e.in_control_s) == ("expired", "timeout", 29.0, 0.0)


def test_a_request_nobody_took_suspends_the_run(tmp_path: Path) -> None:
    d = _run(
        tmp_path,
        "run_PENDING",
        [
            _ask(1, "req_1"),
            {"ts": _ts(2), "event": "escalation.pending", "request": "req_1", "why": "nobody"},
            _end(2.1, "escalated"),
        ],
        transitions=[(1, "PAUSED", "req_1")],
    )
    i = _one(d)
    assert i.kind == "unanswered" and i.queued_s == pytest.approx(1.1)
    assert run_metrics(read_run(d)).status == "escalated"


def test_a_handback_no_checkpoint_held_on_is_returned_and_asked_again(tmp_path: Path) -> None:
    d = _run(
        tmp_path,
        "run_LOST",
        [
            _ask(1, "req_1"),
            {
                "ts": _ts(3),
                "event": "handoff.handed_back",
                "request": "req_1",
                "by": "ops",
                "decision": "hand_back",
            },
            {"ts": _ts(3.5), "event": "handoff.lost", "step": "s1", "attempt": 2},
            _ask(4, "req_2"),
            {
                "ts": _ts(6),
                "event": "handoff.handed_back",
                "request": "req_2",
                "by": "ops",
                "decision": "hand_back",
            },
            {"ts": _ts(6.2), "event": "handoff.resumed", "request": "req_2", "at": "s2"},
            _end(8, "success"),
        ],
        transitions=[
            (1, "PAUSED", "req_1"),
            (2, "HUMAN_IN_CONTROL", "req_1"),
            (3, "RESUMING", "req_1"),
            (4, "PAUSED", "req_2"),
            (5, "HUMAN_IN_CONTROL", "req_2"),
            (6, "RESUMING", "req_2"),
        ],
        humans=[
            {"ts": _ts(2.5), "request_id": "req_1", "source": "browser", "action": "click"},
            {"ts": _ts(5.5), "request_id": "req_2", "source": "browser", "action": "click"},
            {"ts": _ts(5.6), "request_id": "req_2", "source": "console", "action": "resume"},
        ],
    )
    first, second = run_metrics(read_run(d)).human.interventions
    assert (first.kind, first.human_s, first.browser_actions) == ("returned", 2.0, 1)
    assert (second.kind, second.resumed_at, second.browser_actions) == ("recovery", "s2", 1)
    assert (second.queued_s, second.in_control_s) == (1.0, 1.0)


def test_discovery_without_a_channel_asked_nobody(tmp_path: Path) -> None:
    d = _run(
        tmp_path,
        "run_NOCHANNEL",
        [
            {"ts": _ts(1), "event": "escalation.requested", "reason_code": "STUCK", "note": "no"},
            _end(1.1, "escalated"),
        ],
        kind="discovery",
    )
    i = _one(d)
    assert (i.kind, i.reason, i.human_s) == ("not_asked", "STUCK", 0.0)


def test_a_discovery_abort_without_a_request_belongs_to_the_last_ask(tmp_path: Path) -> None:
    d = _run(
        tmp_path,
        "run_DABORT",
        [
            _ask(1, "req_1"),
            {
                "ts": _ts(3),
                "event": "handoff.handed_back",
                "turn": 2,
                "request": "req_1",
                "by": "anil",
                "decision": "hand_back",
            },
            _ask(5, "req_2"),
            {"ts": _ts(7), "event": "handoff.aborted", "turn": 4, "by": "anil", "why": "enough"},
            _end(7.5, "stopped"),
        ],
        kind="discovery",
    )
    first, second = run_metrics(read_run(d)).human.interventions
    # The discovery loop always carries on with its next turn after a handback.
    assert (first.kind, second.kind) == ("recovery", "abort")


def test_a_request_still_open_when_the_run_ended(tmp_path: Path) -> None:
    d = _run(tmp_path, "run_OPEN", [_ask(1, "req_1")], transitions=[(1, "PAUSED", "req_1")])
    assert _one(d).kind == "open"


# -- the report --------------------------------------------------------------------------


def _m(name: str, kind: str, human_s: float, *, by: str = "ops") -> RunMetrics:
    i = Intervention(
        request_id=f"req_{name}",
        kind=kind,
        decided_by=by,
        requested_at=_ts(1),
        queued_s=human_s,  # type: ignore[arg-type]
    )
    return RunMetrics.model_validate(
        {
            "run_id": name,
            "invocation_id": name,
            "kind": "replay",
            "tenant_id": "t",
            "capability": "cap_x",
            "capability_id": "cap_X",
            "capability_version": 1,
            "started_at": _ts(0),
            "ended_at": _ts(1),
            "duration_s": 1.0,
            "status": "completed",
            "outcome": "success",
            "human": {"handoffs": 1, "resumed": 1, "wait_s": human_s, "interventions": [i]},
        }
    )


def test_the_summary_keeps_each_kind_apart_and_prices_the_time() -> None:
    runs = [_m("a", "approval", 10), _m("b", "approval", 30), _m("c", "manual_completion", 360)]
    plain = RunMetrics.model_validate(
        {**_m("d", "approval", 0).model_dump(), "run_id": "d", "human": {}}
    )
    data = summarise([*runs, plain], HumanPrice(operator_hour=Decimal(36)))
    g = data["groups"]["all"]
    assert (g["runs"], g["runs_requiring_human"], g["runs_with_intervention"]) == (4, 3, 3)
    assert g["human_intervention_rate"] == 0.75
    approval = g["by_kind"]["approval"]
    assert (approval["count"], approval["mean_human_s"], approval["p95_human_s"]) == (2, 20.0, 30.0)
    assert approval["mean_cost_usd"] == "0.2000"  # 20 s at $36/h
    assert g["by_kind"]["manual_completion"]["mean_cost_usd"] == "3.6000"
    assert g["decided"]["count"] == 3
    text = "\n".join(markdown(data))
    assert "manual completion (the person finished)" in text and "$36/h" in text


def test_without_a_price_time_is_reported_in_seconds_only() -> None:
    data = summarise([_m("a", "abort", 5)])
    assert "mean_cost_usd" not in data["groups"]["all"]["by_kind"]["abort"]
    assert "Mean cost" not in "\n".join(markdown(data))


def test_the_committed_price_table_prices_an_operator_hour() -> None:
    human = load_prices(REPO_ROOT / "bench" / "pricing.yaml").human
    assert human is not None and human.operator_hour > 0 and human.source


def test_cua_metrics_humans_writes_its_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    code = main(
        [
            "metrics",
            "humans",
            "--runs-dir",
            str(EVIDENCE / "replay-escalation"),
            "--runs-dir",
            str(EVIDENCE / "discovery-open-subaccount"),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    data = json.loads((out / "humans.json").read_text(encoding="utf-8"))
    assert set(data["groups"]["all"]["by_kind"]) == {"approval", "recovery"}
    assert "| anil | 1 |" in (out / "humans.md").read_text(encoding="utf-8")
    assert "# Human intervention" in capsys.readouterr().out


# -- the benchmark row -----------------------------------------------------------------


def test_a_benchmark_row_records_its_human_time() -> None:
    fields = human_fields(EVIDENCE / "replay-escalation")
    assert fields["human_interventions"] == 1 and fields["human_kinds"] == {"recovery": 1}
    assert fields["human_action_count"] == 1 and fields["human_wait_s"] > 1
    # An answer from the idempotency store asked nobody this time.
    assert human_fields(EVIDENCE / "replay-escalation", cached=True) == {}


def test_benchmark_stats_count_runs_requiring_a_person() -> None:
    def row(**extra: Any) -> Row:
        return Row.model_validate(
            {
                "session_id": "s",
                "task_id": "t",
                "strategy": "inter_cua_replay",
                "repetition": 1,
                "started_at": _ts(0),
                "match": "exact",
                "success": True,
                "outcome": "success",
                "truth": "answer",
                "duration_s": 1.0,
                **extra,
            }
        )

    s = stats(
        [
            row(human_interventions=1, human_wait_s=4.0, human_kinds={"approval": 1}),
            row(escalated=True),
            row(),
        ]
    )
    assert (s["runs_requiring_human"], s["human_interventions"], s["human_wait_s"]) == (2, 1, 4.0)
    assert s["human_kinds"] == {"approval": 1}
