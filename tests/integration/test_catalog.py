"""A calling agent's view, end to end: invoke by name, get ``escalated``, get
consent, and ``cua resume`` completes the commit.

Each command is its own process, as it would be for an agent calling the
CLI as a tool: the first exits leaving its browser up, and the second
attaches to that browser and carries on from the checkpoint the screen proves.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cua.surface.playwright_surface import BrowserProcess
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.browser

ARGS = {"member_id": "10003", "initial_deposit": 250.00}
AS_TYPED = ["--input", "member_id=10003", "--input", "initial_deposit=250.0"]


def test_an_escalated_invocation_is_completed_by_resume_with_fresh_consent(
    mockapp_url: str, tmp_path: Path
) -> None:
    tenant = tmp_path / "tenant.yaml"
    tenant.write_text(
        (REPO_ROOT / "tenants" / "local.yaml")
        .read_text()
        .replace("http://127.0.0.1:8000", mockapp_url)
        .replace(".cua/approval-signing.key", (tmp_path / "signing.key").as_posix())
    )
    env = {**os.environ, "CUA_SECRET_MOCKCORE_OPERATOR": "operator:operator"}
    runs = ["--runs-dir", str(tmp_path / "runs")]

    def cua(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "cua.cli", *argv],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
            timeout=120,
        )

    first = cua(
        "catalog",
        "invoke",
        "open_subaccount",
        "--args",
        json.dumps(ARGS),
        "--tenant",
        str(tenant),
        "--handoff",
        "--handoff-wait",
        "0",
        "--no-screenshots",
        *runs,
    )
    assert first.returncode == 3, first.stderr
    escalated = json.loads(first.stdout)
    assert escalated["kind"] == "escalated" and escalated["reason"] == "NEEDS_APPROVAL"
    assert escalated["side_effect"] == "none" and escalated["step_id"] == "review.submit"
    run_dir = Path(escalated["evidence"]["run_dir"])
    session = json.loads((run_dir / "session.json").read_text())
    browser = BrowserProcess(session["pid"], session["cdp_url"], Path(session["profile"]))
    try:
        assert browser.alive()

        # Consent for other inputs is not consent: refused, and the run still waits.
        other = cua(
            "approval-token",
            str(REPO_ROOT / "capabilities" / "open_subaccount.json"),
            "--tenant",
            str(tenant),
            "--input",
            "member_id=10003",
            "--input",
            "initial_deposit=999.00",
            "--by",
            "reviewer",
        )
        assert other.returncode == 0, other.stderr
        refused = cua(
            "resume", escalated["resume_token"], "--approval-token", other.stdout.strip(), *runs
        )
        assert refused.returncode == 64 and "approval refused" in refused.stderr
        assert browser.alive()

        # The inputs are the invocation's as replay saw them: 250.00 typed as a number.
        token = cua(
            "approval-token",
            str(REPO_ROOT / "capabilities" / "open_subaccount.json"),
            "--tenant",
            str(tenant),
            *AS_TYPED,
            "--by",
            "reviewer",
        )
        assert token.returncode == 0, token.stderr
        done = cua(
            "resume",
            escalated["resume_token"],
            "--approval-token",
            token.stdout.strip(),
            "--approved-by",
            "reviewer",
            *runs,
        )
        assert done.returncode == 0, done.stderr
        result = json.loads(done.stdout)
        assert result["kind"] == "success" and result["side_effect"] == "committed"
        assert result["outputs"]["reference_number"].startswith("REF-10003-")
        assert result["handoffs"][0]["reason"] == "NEEDS_APPROVAL"
        assert result["handoffs"][0]["resumed_at"] == "review.submit"
        events = [json.loads(x) for x in (run_dir / "log.jsonl").read_text().splitlines()]
        approved = [e for e in events if e["event"] == "policy.approved"]
        assert len(approved) == 1 and approved[0]["via"] == "token"
        assert approved[0]["approved_by"] == "reviewer"
        assert token.stdout.strip() not in (run_dir / "log.jsonl").read_text()
        assert not browser.alive()

        # One consent, one commit: the same token cannot drive a second one.
        again = cua(
            "catalog",
            "invoke",
            "open_subaccount",
            "--args",
            json.dumps(ARGS),
            "--tenant",
            str(tenant),
            "--approval-token",
            token.stdout.strip(),
            *runs,
        )
        assert again.returncode == 1
        assert "already used by" in json.loads(again.stdout)["message"]
    finally:
        browser.kill()
