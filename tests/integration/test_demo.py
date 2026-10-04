"""All seven demo stages use real CLI subprocesses, browser and operator console."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from cua.demo.report import generate
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.browser


def test_phase18_demo_end_to_end(tmp_path, browser_session):
    out = tmp_path / "session"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/demo/run_demo.py"),
            "--out",
            str(out),
            "--repetitions",
            "2",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((out / "summary.json").read_text())
    assert summary["replay_invocations"] == 2
    assert summary["replay_model_calls"] == 0
    assert summary["commits"] == 1
    assert len(summary["stages"]) == 7
    assert summary["security"]["blocked_count"] == 1
    assert "cat1." not in (out / "manifest.json").read_text()
    manifest = json.loads((out / "manifest.json").read_text())
    discovery = next(c for c in manifest["calls"] if c["label"] == "discovery")
    discovery_run = out / discovery["result"]["run_dir"]
    calls_path = discovery_run / "model_calls.jsonl"
    assert calls_path.is_file()
    calls = [json.loads(line) for line in calls_path.read_text().splitlines() if line.strip()]
    assert calls, "discovery must record its model calls"
    evidence_paths = list((out / "workspace/runs").glob("*/model_calls.jsonl"))
    assert calls_path in evidence_paths
    for call in manifest["calls"]:
        if call["label"] == "discovery" or "run_dir" not in call["result"]:
            continue
        run = out / call["result"]["run_dir"]
        assert run.is_dir(), call["label"]
        # Resume completes the same run that the drift call escalated.
        expected_kind = "success" if call["label"] == "drift" else call["result"]["kind"]
        assert json.loads((run / "result.json").read_text())["kind"] == expected_kind
        model_calls = run / "model_calls.jsonl"
        assert not model_calls.exists() or not model_calls.read_text().strip(), call["label"]
    first = (out / "summary.md").read_bytes()
    generate(out)
    assert (out / "summary.md").read_bytes() == first
