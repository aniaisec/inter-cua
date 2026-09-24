"""Run observability: canonical events, per-run metrics, health and cost.

Read from the committed evidence where it covers the case (a real replay, a
real live-model discovery, a real handoff), and from small run directories
built here for the cases the evidence does not have (drift, a torn line).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cua.cli import main
from cua.observability.correlation import header_from
from cua.observability.cost import PriceTable, load_prices
from cua.observability.events import EVENT_TYPES
from cua.observability.health import all_health, capability_health, percentile
from cua.observability.metrics import BUCKETS, RunMetrics, run_metrics
from cua.observability.recorder import find_runs, locate_run, read_run
from tests.conftest import REPO_ROOT

EVIDENCE = REPO_ROOT / "evidence"
PRICES = load_prices(REPO_ROOT / "bench" / "pricing.yaml")


def _evidence_runs() -> list[Path]:
    return sorted(p for p in EVIDENCE.iterdir() if (p / "run.json").is_file())


# -- canonical events from the committed evidence -------------------------------


@pytest.mark.parametrize("run_dir", _evidence_runs(), ids=lambda p: p.name)
def test_every_committed_run_reads_into_canonical_events(run_dir: Path) -> None:
    record = read_run(run_dir, PRICES)
    assert record.warnings == []
    assert record.events, "a run with a log has events"
    assert {e.type for e in record.events} <= set(EVENT_TYPES)
    # Every event carries the run's correlation, and says where it came from.
    corr = record.header.correlation
    for e in record.events:
        assert (e.run_id, e.invocation_id, e.tenant_id) == (
            corr.run_id,
            corr.invocation_id,
            corr.tenant_id,
        )
        assert (e.capability_id, e.capability_version) == (
            corr.capability_id,
            corr.capability_version,
        )
        assert e.source.split(":")[0] in {
            "log.jsonl",
            "model_calls.jsonl",
            "human_actions.jsonl",
            "result.json",
        }
    stamps = [e.timestamp for e in record.events]
    assert stamps == sorted(stamps)
    # Exactly one way in and (for a finished run) one way out.
    assert record.of("run.started")
    assert len(record.of("run.completed", "run.failed", "run.escalated")) >= 1


@pytest.mark.parametrize("run_dir", _evidence_runs(), ids=lambda p: p.name)
def test_the_time_breakdown_adds_up_to_the_wall_clock(run_dir: Path) -> None:
    m = run_metrics(read_run(run_dir, PRICES))
    assert set(m.time) == set(BUCKETS)
    assert all(v >= 0 for v in m.time.values())
    assert sum(m.time.values()) == pytest.approx(m.duration_s, abs=0.01)


def test_a_clean_replay_is_explained_step_by_step() -> None:
    m = run_metrics(read_run(EVIDENCE / "replay-success", PRICES))
    assert (m.kind, m.capability, m.capability_version) == ("replay", "member_savings_balance", 3)
    assert (m.status, m.outcome, m.side_effect) == ("completed", "success", "none")
    assert m.llm.calls == 0 and m.llm.estimated_cost_usd == Decimal(0)
    assert m.time["llm"] == 0 and m.time["human"] == 0
    assert [s.step_id for s in m.steps] == [
        "login.username",
        "login.password",
        "login.submit",
        "search.member_id",
        "search.submit",
    ]
    assert all(s.status == "completed" and not s.fell_back for s in m.steps)
    for s in m.steps:
        assert s.locate_s + s.act_s + s.verify_s == pytest.approx(s.duration_s, abs=0.01)
    # The locators the result reports are the ones the events show.
    result = json.loads((EVIDENCE / "replay-success" / "result.json").read_text("utf-8"))
    assert m.locators == result["locator_rungs_used"]
    assert (m.recoveries, m.locator_drift, m.locator_failures, m.human.intervened) == (
        0,
        0,
        0,
        False,
    )


def test_a_live_discovery_shows_its_model_calls_tokens_and_estimated_cost() -> None:
    run_dir = EVIDENCE / "discovery-member-savings-balance"
    m = run_metrics(read_run(run_dir, PRICES))
    calls = [
        json.loads(line)
        for line in (run_dir / "model_calls.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    assert m.kind == "discovery" and m.status == "completed" and m.outcome == "done"
    assert m.llm.calls == len(calls) == 6
    assert m.llm.input_tokens == sum(c["usage"]["input_tokens"] for c in calls)
    assert m.llm.thinking_tokens == sum(c["usage"].get("thinking_tokens", 0) for c in calls)
    assert m.llm.models == ["gemini-3.8-flash"]
    expected = sum(
        (
            PRICES.cost(
                "gemini",
                "gemini-3.8-flash",
                input_tokens=c["usage"]["input_tokens"],
                output_tokens=c["usage"]["output_tokens"],
                thinking_tokens=c["usage"].get("thinking_tokens", 0),
            )
            or Decimal(0)
            for c in calls
        ),
        Decimal(0),
    )
    assert m.llm.estimated_cost_usd == expected > 0
    # These calls were logged before the loop timed them: the wait is
    # inferred, and says so.
    assert m.llm.wait_measured is False
    assert all(e.derived for e in read_run(run_dir).of("llm.started"))
    assert m.time["llm"] == pytest.approx(m.llm.wait_s, abs=0.01)
    assert m.time["llm"] > 0.8 * m.duration_s


def test_a_handoff_shows_who_intervened_how_long_the_run_waited_and_what_it_committed() -> None:
    m = run_metrics(read_run(EVIDENCE / "replay-escalation", PRICES))
    assert m.status == "completed" and m.inject == "modal_dialog"
    assert m.why is None, "a run that completed is not explained by the failure it got past"
    h = m.human
    assert (h.handoffs, h.resumed, h.aborted) == (1, 1, 0)
    assert h.actions == 3 and h.intervened
    assert h.wait_s > 0 and m.time["human"] == pytest.approx(h.wait_s, abs=0.01)
    assert m.side_effect == "committed"
    failed = [s for s in m.steps if s.status == "failed"]
    assert [(s.step_id, s.error_code) for s in failed] == [
        ("member.open_sub_account", "ACTION_FAILED")
    ]
    # The control was found; the click itself hung. That is time acting.
    assert failed[0].act_s > failed[0].locate_s


# -- built run directories -------------------------------------------------------

T0 = "2026-09-24T10:00:00.000Z"


def _ts(seconds: float) -> str:
    whole = int(seconds)
    ms = round((seconds - whole) * 1000)
    return f"2026-09-24T10:00:{whole:02d}.{ms:03d}Z"


def _write_run(
    root: Path,
    log: list[dict[str, Any]],
    *,
    run: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    calls: list[dict[str, Any]] | None = None,
    humans: list[dict[str, Any]] | None = None,
    torn: bool = False,
) -> Path:
    run_id = (run or {}).get("run_id", "run_TEST")
    d = root / run_id
    d.mkdir(parents=True)
    header = run or {
        "run_id": run_id,
        "kind": "replay",
        "started_at": T0,
        "capability": {"id": "cap_X", "name": "cap_x", "version": 2},
        "tenant": {"id": "t1"},
        "request": {"inputs": {}, "idempotency_key": None, "inject": None},
    }
    (d / "run.json").write_text(json.dumps(header), encoding="utf-8")
    lines = [json.dumps({"seq": i + 1, **line}) for i, line in enumerate(log)]
    text = "\n".join(lines) + "\n" + ('{"seq": 99, "ts": "2026-09-24T10:0' if torn else "")
    (d / "log.jsonl").write_text(text, encoding="utf-8")
    if result is not None:
        (d / "result.json").write_text(json.dumps(result), encoding="utf-8")
    if calls is not None:
        (d / "model_calls.jsonl").write_text(
            "\n".join(json.dumps(c) for c in calls) + "\n", encoding="utf-8"
        )
    if humans is not None:
        (d / "human_actions.jsonl").write_text(
            "\n".join(json.dumps(h) for h in humans) + "\n", encoding="utf-8"
        )
    return d


def _drifted(tmp_path: Path, *, key: str | None = None) -> Path:
    return _write_run(
        tmp_path,
        [
            {"ts": _ts(0.5), "event": "run.start", "capability": "cap_x"},
            {"ts": _ts(1), "event": "step.start", "step": "a.click", "action": "click"},
            {
                "ts": _ts(1.2),
                "event": "locator.resolved",
                "target": "a.click",
                "rung": "near_text",
                "recorded": "role_name",
            },
            {"ts": _ts(1.5), "event": "action.done", "step": "a.click", "ms": 100},
            {"ts": _ts(2), "event": "step.passed", "step": "a.click"},
            {"ts": _ts(2.5), "event": "step.start", "step": "b.click", "action": "click"},
            {"ts": _ts(3), "event": "recovery.retry", "step": "b.click", "code": "interstitial"},
            {
                "ts": _ts(4),
                "event": "replay.failed",
                "step": "b.click",
                "code": "LOCATOR_UNRESOLVED",
                "message": "no rung found b.click",
            },
            {"ts": _ts(4.5), "event": "run.end", "kind": "failure", "code": "LOCATOR_UNRESOLVED"},
        ],
        run={
            "run_id": "run_DRIFT",
            "kind": "replay",
            "started_at": T0,
            "capability": {"id": "cap_X", "name": "cap_x", "version": 2},
            "tenant": {"id": "t1"},
            "request": {"idempotency_key": key, "inject": None},
        },
        result={"kind": "failure", "code": "LOCATOR_UNRESOLVED", "side_effect": "none"},
    )


def test_drift_a_missing_control_and_a_recovery_each_have_their_own_event(tmp_path: Path) -> None:
    record = read_run(_drifted(tmp_path))
    types = [e.type for e in record.events]
    assert types.count("locator.drift") == 1
    drift = record.of("locator.drift")[0]
    assert drift.step_id == "a.click"
    assert drift.attrs == {"cause": "fallback", "from": "role_name", "to": "near_text"}
    assert [e.step_id for e in record.of("locator.failed")] == ["b.click"]
    assert [e.attrs["action"] for e in record.of("recovery.started")] == ["retry"]

    m = run_metrics(record)
    assert (m.status, m.outcome, m.failed_step) == (
        "failed",
        "failure:LOCATOR_UNRESOLVED",
        "b.click",
    )
    assert m.why == "no rung found b.click"
    assert (m.locator_fallbacks, m.locator_drift, m.locator_failures, m.recoveries) == (1, 1, 1, 1)
    assert m.steps[0].fell_back and m.steps[0].rung == "near_text"
    # 0.5 s to the first event, which is browser startup.
    assert m.time["startup"] == pytest.approx(0.5)
    assert m.time["act"] == pytest.approx(0.1)
    assert m.time["locate"] == pytest.approx(0.4 + 0.5)  # a: 1.0-1.4; b: 2.5-3.0
    assert m.time["recovery"] == pytest.approx(1.0)  # the retry, until b fails
    assert m.time["evidence"] == pytest.approx(0.5)  # a passed at 2.0, b started at 2.5
    assert sum(m.time.values()) == pytest.approx(m.duration_s) == pytest.approx(4.5)


def test_a_retried_request_is_one_invocation_across_its_runs(tmp_path: Path) -> None:
    corr = read_run(_drifted(tmp_path, key="order-7")).header.correlation
    assert corr.invocation_id == "idem:order-7" != corr.run_id
    plain = header_from({"run_id": "run_A", "kind": "replay", "capability": {}}, fallback_id="x")
    assert plain.correlation.invocation_id == "run_A"


def test_a_torn_last_line_is_reported_and_the_rest_of_the_run_still_reads(tmp_path: Path) -> None:
    d = _write_run(
        tmp_path,
        [
            {"ts": _ts(1), "event": "run.start"},
            {"ts": _ts(2), "event": "step.start", "step": "a", "action": "click"},
        ],
        torn=True,
    )
    record = read_run(d)
    assert record.warnings == ["log.jsonl:3: not JSON (a torn write?)"]
    m = run_metrics(record)
    assert m.status == "incomplete" and m.outcome == "incomplete"
    assert m.warnings == record.warnings


def _discovery(tmp_path: Path, calls: list[dict[str, Any]], provider: str = "gemini") -> Path:
    return _write_run(
        tmp_path,
        [
            {"ts": _ts(1), "event": "run.start", "provider": provider, "model": "m"},
            {"ts": _ts(1.5), "event": "observe", "turn": 0},
            {
                "ts": _ts(3),
                "event": "agent.decision",
                "turn": 1,
                "tool": "type",
                "input": {"ref": "n1", "text": "SENTINEL-TYPED-VALUE"},
                "reason": "enter the id",
                "target": "textbox",
            },
            {"ts": _ts(3.2), "event": "action.done", "turn": 1, "tool": "type", "ms": 50},
            {"ts": _ts(4), "event": "run.end", "kind": "done", "reason": None, "message": "ok"},
        ],
        run={
            "run_id": "run_DISC",
            "started_at": _ts(1),
            "goal": {"goal": "find it", "name": "cap_y"},
            "tenant": {"id": "t1"},
            "provider": provider,
            "model": "m",
            "entry_url": "http://127.0.0.1:8000/login?inject=slow_load",
        },
        calls=calls,
        humans=[
            {
                "ts": _ts(3.5),
                "source": "browser",
                "action": "change",
                "target": {"role": "textbox", "name": "Member ID"},
                "value": "SENTINEL-HUMAN-VALUE",
                "url": "http://127.0.0.1:8000/?q=SENTINEL-QUERY",
            }
        ],
    )


def test_a_timed_model_call_is_measured_and_priced_per_call(tmp_path: Path) -> None:
    call = {
        "ts": _ts(2.9),
        "turn": 1,
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "cache_read_input_tokens": 4000,
            "cache_creation_input_tokens": 0,
        },
        "ms": 1200,
    }
    record = read_run(_discovery(tmp_path, [call], "anthropic"), PRICES)
    started, done = record.of("llm.started")[0], record.of("llm.completed")[0]
    assert not started.derived and started.timestamp == _ts(1.7)
    # Claude reports cached prompt tokens beside input_tokens: 5000 prompt tokens.
    assert done.attrs["usage"]["input"] == 5000
    m = run_metrics(record)
    assert m.llm.wait_measured and m.llm.wait_s == pytest.approx(1.2)
    assert m.llm.estimated_cost_usd == PRICES.cost(
        "anthropic",
        "claude-sonnet-5",
        input_tokens=5000,
        output_tokens=100,
        cache_read_tokens=4000,
    )
    assert m.inject == "slow_load"
    assert m.human.actions == 1 and m.human.intervened


def test_an_unpriced_model_is_reported_unpriced_never_free(tmp_path: Path) -> None:
    call = {"ts": _ts(2.9), "turn": 1, "provider": "gemini", "model": "gemini-0-new", "usage": {}}
    m = run_metrics(read_run(_discovery(tmp_path, [call]), PRICES))
    assert m.llm.calls == 1 and m.llm.estimated_cost_usd is None
    # Without a price table nothing is priced either.
    assert run_metrics(read_run(tmp_path / "run_DISC")).llm.estimated_cost_usd is None


def test_a_scripted_model_costs_nothing(tmp_path: Path) -> None:
    call = {"ts": _ts(2.9), "turn": 1, "provider": "scripted", "model": "scripted", "usage": {}}
    m = run_metrics(read_run(_discovery(tmp_path, [call], "scripted"), PriceTable()))
    assert m.llm.estimated_cost_usd == Decimal(0)


def test_events_carry_no_typed_value_and_no_query_string(tmp_path: Path) -> None:
    call = {"ts": _ts(2.9), "turn": 1, "provider": "gemini", "model": "m", "usage": {}}
    record = read_run(_discovery(tmp_path, [call]))
    dumped = "\n".join(e.model_dump_json() for e in record.events)
    assert "SENTINEL" not in dumped
    action = record.of("human.action")[0]
    assert action.attrs["url"] == "http://127.0.0.1:8000/"
    assert action.attrs["target"] == {"role": "textbox", "name": "Member ID"}


# -- health --------------------------------------------------------------------------


def _m(**kw: Any) -> RunMetrics:
    base: dict[str, Any] = {
        "run_id": "r",
        "invocation_id": "r",
        "kind": "replay",
        "tenant_id": "t",
        "capability": "cap",
        "capability_id": "c",
        "capability_version": 1,
        "started_at": T0,
        "ended_at": T0,
        "duration_s": 1.0,
        "status": "completed",
        "outcome": "success",
    }
    return RunMetrics(**{**base, **kw})


def test_health_rates_leave_out_injected_faults_and_refusals_but_count_them() -> None:
    runs = [
        _m(run_id="ok1", ended_at=_ts(1), duration_s=2.0),
        _m(run_id="ok2", ended_at=_ts(3), duration_s=4.0, outcome="business_outcome:NOT_FOUND"),
        _m(
            run_id="bad",
            ended_at=_ts(2),
            status="failed",
            outcome="failure:LOCATOR_UNRESOLVED",
            locator_failures=1,
            locator_drift=1,
        ),
        _m(run_id="inj", status="failed", outcome="failure:APP_ERROR", inject="server_error"),
        _m(run_id="ref", status="failed", outcome="failure:POLICY_BLOCKED", consent_missing=True),
        _m(run_id="v2", capability_version=2),
        _m(run_id="disc", kind="discovery"),
    ]
    h = capability_health(runs, "cap", version=1)
    assert h is not None
    assert (h.runs, h.injected_excluded, h.refused) == (3, 1, 1)
    assert h.success_rate == pytest.approx(2 / 3, abs=1e-4)
    third = pytest.approx(1 / 3, abs=1e-4)
    assert h.failure_rate == h.locator_failure_rate == h.drift_rate == third
    assert h.last_success is not None and h.last_success.run_id == "ok2"
    assert h.last_failure is not None and h.last_failure.run_id == "bad"
    assert h.failures == {"failure:LOCATOR_UNRESOLVED": 1}
    assert h.mean_latency_s == pytest.approx((2 + 4 + 1) / 3, abs=1e-3)

    with_faults = capability_health(runs, "cap", version=1, include_injected=True)
    assert with_faults is not None and with_faults.runs == 4
    assert capability_health(runs, "nope") is None
    assert [(x.capability, x.version, x.runs) for x in all_health(runs)] == [
        ("cap", 1, 3),
        ("cap", 2, 1),
    ]


def test_percentile_is_nearest_rank() -> None:
    assert percentile([], 95) == 0.0
    assert percentile([3.0, 1.0, 2.0], 50) == 2.0
    assert percentile([float(i) for i in range(1, 21)], 95) == 19.0


# -- finding runs, and the CLI -------------------------------------------------------


def test_runs_are_found_once_each_and_by_id(tmp_path: Path) -> None:
    first = _drifted(tmp_path / "a")
    copy = tmp_path / "b" / "copy-of-drift"
    copy.parent.mkdir()
    copy.mkdir()
    for f in first.iterdir():
        (copy / f.name).write_bytes(f.read_bytes())
    assert list(find_runs([tmp_path / "a", tmp_path / "b"])) == [first]
    assert locate_run("run_DRIFT", [tmp_path]) in (first, copy)
    assert locate_run(str(copy), []) == copy
    assert locate_run("run_NOPE", [tmp_path]) is None


def test_cua_metrics_run_explains_a_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _drifted(tmp_path)
    assert main(["metrics", "run", "run_DRIFT", "--runs-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "failure:LOCATOR_UNRESOLVED (failed)" in out
    assert "why        no rung found b.click" in out
    assert "1 by a fallback rung, 1 drift, 1 not found" in out
    assert "near_text*" in out

    assert main(["metrics", "run", "run_DRIFT", "--runs-dir", str(tmp_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["run_id"] == "run_DRIFT" and data["locator_failures"] == 1

    assert main(["metrics", "run", "run_DRIFT", "--runs-dir", str(tmp_path), "--events"]) == 0
    types = [json.loads(line)["type"] for line in capsys.readouterr().out.splitlines()]
    assert types[0] == "run.started" and types[-1] == "run.failed"

    assert main(["metrics", "run", "run_NOPE", "--runs-dir", str(tmp_path)]) == 64


def test_cua_metrics_capability_and_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _drifted(tmp_path)
    assert main(["metrics", "capability", "cap_x", "--runs-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "| cap_x | 2 | 1 | 0.0% | 100.0% |" in out
    assert "did not complete: failure:LOCATOR_UNRESOLVED x1" in out
    assert main(["metrics", "capability", "other", "--runs-dir", str(tmp_path)]) == 64

    out_dir = tmp_path / "out"
    assert main(["metrics", "report", "--runs-dir", str(tmp_path), "--out", str(out_dir)]) == 0
    data = json.loads((out_dir / "metrics.json").read_text("utf-8"))
    assert data["runs"] == 1 and data["model_spend"]["replay"]["llm_calls"] == 0
    assert (out_dir / "metrics.md").read_text("utf-8").startswith("# Capability metrics")


def _blocked(tmp_path: Path, run_id: str, end_code: str) -> Path:
    return _write_run(
        tmp_path,
        [
            {"ts": _ts(1), "event": "run.start"},
            {"ts": _ts(2), "event": "step.start", "step": "review.submit", "action": "click"},
            {"ts": _ts(3), "event": "policy.needs_approval", "step": "review.submit", "rule": "r"},
            {"ts": _ts(4), "event": "run.end", "kind": "failure", "code": end_code},
        ],
        run={
            "run_id": run_id,
            "kind": "replay",
            "started_at": T0,
            "capability": {"id": "cap_X", "name": "cap_x", "version": 3},
            "tenant": {"id": "t1"},
            "request": {},
        },
    )


def test_only_a_request_stopped_for_want_of_approval_counts_as_refused(tmp_path: Path) -> None:
    refused = run_metrics(read_run(_blocked(tmp_path, "run_R", "POLICY_BLOCKED")))
    assert refused.consent_missing and refused.policy_blocks == 1
    # Handed to a person who said no: a human decision, and rated as one.
    aborted = run_metrics(read_run(_blocked(tmp_path, "run_A", "ESCALATION_ABORTED")))
    assert not aborted.consent_missing


def test_capability_health_prints_each_version_once_and_all_versions_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _drifted(tmp_path)  # cap_x v2
    _write_run(
        tmp_path,
        [
            {"ts": _ts(1), "event": "run.start"},
            {"ts": _ts(2), "event": "run.end", "kind": "success"},
        ],
        run={
            "run_id": "run_V3",
            "kind": "replay",
            "started_at": T0,
            "capability": {"id": "cap_X", "name": "cap_x", "version": 3},
            "tenant": {"id": "t1"},
            "request": {},
        },
    )
    # A discovery under the same name has no version and is not a replay.
    _write_run(
        tmp_path,
        [{"ts": _ts(1), "event": "run.start"}, {"ts": _ts(2), "event": "run.end", "kind": "done"}],
        run={"run_id": "run_D", "goal": {"name": "cap_x"}, "tenant": {"id": "t1"}},
    )
    assert main(["metrics", "capability", "cap_x", "--runs-dir", str(tmp_path)]) == 0
    rows = [line for line in capsys.readouterr().out.splitlines() if line.startswith("| cap_x")]
    assert [r.split(" | ")[1] for r in rows] == ["2", "3", "all"]
