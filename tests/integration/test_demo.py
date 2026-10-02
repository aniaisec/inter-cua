"""All seven demo stages use real CLI subprocesses, browser and operator console."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from cua.demo.report import generate
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.browser


def test_phase18_demo_end_to_end(tmp_path):
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
    assert (out / "workspace/runs").glob("*/model_calls.jsonl") is not None
    first = (out / "summary.md").read_bytes()
    generate(out)
    assert (out / "summary.md").read_bytes() == first
