"""Demo evidence must fail closed rather than report unsupported success."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cua.demo.report import STAGES, generate, main
from cua.demo.runner import Demo, prepare
from tests.conftest import REPO_ROOT


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def session(tmp_path):
    root = tmp_path / "demo"
    replay = "workspace/runs/replay"
    resumed = "workspace/runs/resumed"
    handoffs = [
        {
            "resumed_after_checkpoint": "cp.done",
            "decided_by": "evidence-bot",
            "human_actions_count": 1,
        }
    ]
    calls = []
    for label, code, result in (
        ("discovery", 0, {"kind": "done"}),
        ("draft-refused", 1, {"kind": "failure", "code": "POLICY_BLOCKED"}),
        ("describe", 0, {}),
        ("approve", 0, {}),
        ("drift", 3, {"kind": "escalated", "reason": "STUCK", "step_id": "search.submit"}),
        ("resume", 0, {"kind": "success", "run_dir": resumed, "handoffs": handoffs}),
        ("no-consent", 1, {"kind": "failure", "code": "POLICY_BLOCKED", "side_effect": "none"}),
        ("valid-consent", 0, {"kind": "success", "side_effect": "committed"}),
        ("reused-consent", 1, {"kind": "failure", "code": "POLICY_BLOCKED", "side_effect": "none"}),
        ("security", 0, {}),
    ):
        calls.append({"label": label, "exit_code": code, "result": result})
    manifest = {
        "schema_version": 1,
        "status": "complete",
        "started_at": "2026-10-02T00:00:00Z",
        "operator": "evidence-bot (scripted)",
        "discovery_provider": "scripted",
        "stages": [{"name": name, "passed": True, "detail": "measured"} for name in STAGES],
        "repetitions": 1,
        "replay_runs": [replay],
        "commits": 1,
        "calls": calls,
        "security_report": "workspace/security-report/summary.json",
    }
    write(root / "manifest.json", manifest)
    write(
        root / replay / "run.json",
        {"kind": "replay", "run_id": "replay", "request": {"idempotency_key": None}},
    )
    write(
        root / replay / "result.json",
        {"kind": "success", "side_effect": "none", "outputs": {"savings_balance": "1411.21"}},
    )
    write(root / resumed / "result.json", {"kind": "success", "handoffs": handoffs})
    cap = {"id": "cap1", "name": "lookup", "version": 1, "steps": [], "contract": {}}
    write(root / "draft.json", {**cap, "approval_state": "draft"})
    write(root / "approved.json", {**cap, "approval_state": "approved"})
    write(
        root / manifest["security_report"],
        {
            "metrics": {
                "attack_count": 1,
                "blocked_count": 1,
                "unsafe_action_count": 0,
                "secret_exposure_count": 0,
                "policy_bypass_count": 0,
                "approval_bypass_count": 0,
                "tenant_isolation_failures": 0,
            }
        },
    )
    return root


def test_report_reads_evidence_and_regenerates_identically(session):
    md, js = generate(session)
    assert json.loads(js.read_text())["replay_model_calls"] == 0
    assert "scripted" in md.read_text()
    first = md.read_bytes(), js.read_bytes()
    generate(session)
    assert first == (md.read_bytes(), js.read_bytes())


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "wrong",
        "model",
        "duplicate",
        "escape",
        "failed",
        "commit",
        "security",
        "snapshot",
        "handoff",
    ],
)
def test_report_refuses_incomplete_or_contradictory_evidence(session, change):
    path = session / "manifest.json"
    data = json.loads(path.read_text())
    run = session / data["replay_runs"][0]
    if change == "missing":
        (run / "result.json").unlink()
    elif change == "wrong":
        write(
            run / "result.json",
            {"kind": "success", "side_effect": "none", "outputs": {"savings_balance": "0"}},
        )
    elif change == "model":
        (run / "model_calls.jsonl").write_text('{"provider":"gemini"}\n')
    elif change == "duplicate":
        data["repetitions"] = 2
        data["replay_runs"] *= 2
    elif change == "escape":
        data["replay_runs"] = ["../outside"]
    elif change == "failed":
        data["status"] = "failed"
    elif change == "commit":
        data["commits"] = 2
    elif change == "security":
        write(
            session / data["security_report"], {"metrics": {"attack_count": 1, "blocked_count": 0}}
        )
    elif change == "snapshot":
        cap = json.loads((session / "approved.json").read_text())
        cap["steps"] = ["tampered"]
        write(session / "approved.json", cap)
    elif change == "handoff":
        write(session / "workspace/runs/resumed/result.json", {"kind": "failure", "handoffs": []})
    write(path, data)
    assert main([str(session)]) == 1
    assert not (session / "summary.md").exists()


def test_prepare_isolates_runtime_state_and_refuses_overwrite(tmp_path):
    out = tmp_path / "demo"
    work, env = prepare(out, "http://127.0.0.1:12345")
    assert (work / "capabilities/open_subaccount.json").read_bytes() == (
        REPO_ROOT / "capabilities/open_subaccount.json"
    ).read_bytes()
    assert "12345" in (work / "tenants/local.yaml").read_text()
    assert len(env["CUA_DEMO_SIGNING_KEY"]) == 64
    assert not (work / ".env").exists()
    (out / "sentinel").write_text("keep")
    with pytest.raises(FileExistsError):
        prepare(out, "http://127.0.0.1:1")
    assert (out / "sentinel").read_text() == "keep"


def test_transcript_redacts_tokens_and_omits_stderr(tmp_path, monkeypatch):
    demo = Demo(tmp_path, tmp_path, {}, "http://127.0.0.1:1")
    secret = "cat1.sensitive.signature"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 3, json.dumps({"kind": "escalated", "resume_token": secret}), secret
        ),
    )
    demo.call("resume", "resume", secret, secret=secret)
    text = (tmp_path / "manifest.json").read_text()
    assert secret not in text
    assert "<redacted token>" in text


def test_report_refuses_idempotency_cached_repetitions(session):
    write(
        session / "workspace/runs/replay/run.json",
        {
            "kind": "replay",
            "run_id": "replay",
            "request": {"idempotency_key": "same"},
        },
    )
    assert main([str(session)]) == 1


def test_report_refuses_one_run_copied_to_two_directories(session):
    import shutil

    shutil.copytree(session / "workspace/runs/replay", session / "workspace/runs/copy")
    path = session / "manifest.json"
    data = json.loads(path.read_text())
    data["repetitions"] = 2
    data["replay_runs"].append("workspace/runs/copy")
    write(path, data)
    assert main([str(session)]) == 1
