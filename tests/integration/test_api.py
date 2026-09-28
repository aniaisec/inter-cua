"""The HTTP API with the real runner, against the mock app.

The acceptance line for the API layer: a client invokes an approved
capability over HTTP and gets the same logical ``ReplayResult`` as the CLI.
Then the commit path end to end: escalated for consent, a token for other
inputs refused with the run still waiting, the right token completing the
commit, a retry answered from the first run; and an escalated run aborted
over HTTP with its browser closed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cua.api.access import AccessConfig, Gate
from cua.api.app import create_app
from cua.api.service import RunService, ServiceSettings
from cua.artifact.store import open_capability
from cua.policy import tokens
from cua.surface.playwright_surface import BrowserProcess
from cua.tenant import load_tenant
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.browser

KEY = "integration-key-" + "k" * 32
HEADERS = {"Authorization": f"Bearer {KEY}", "X-Cua-Tenant": "local"}
LOGICAL = (
    "kind",
    "code",
    "step_id",
    "side_effect",
    "outputs",
    "capability",
    "capability_version",
    "locator_rungs_used",
    "recoveries",
    "handoffs",
)
"""What a result says about the invocation; run ids, timings and evidence
paths differ between any two runs, CLI or not."""


@pytest.fixture
def setup(mockapp_url: str, tmp_path: Path) -> Iterator[tuple[TestClient, Path, dict[str, str]]]:
    tenant = tmp_path / "tenant.yaml"
    tenant.write_text(
        (REPO_ROOT / "tenants" / "local.yaml")
        .read_text()
        .replace("http://127.0.0.1:8000", mockapp_url)
        .replace(".cua/approval-signing.key", (tmp_path / "signing.key").as_posix())
    )
    env = {**os.environ, "CUA_SECRET_MOCKCORE_OPERATOR": "operator:operator", "API_KEY": KEY}
    config = AccessConfig.model_validate(
        {
            "tenants": {
                "local": {"file": str(tenant), "policy": str(REPO_ROOT / "policies/default.yaml")}
            },
            "clients": {
                "agent": {
                    "key": {"provider": "env", "var": "API_KEY"},
                    "tenants": ["local"],
                    "capabilities": ["*"],
                    "scopes": ["read", "invoke", "approve", "operate"],
                }
            },
        }
    )
    service = RunService(
        ServiceSettings(
            runs_dir=tmp_path / "runs", capabilities_dir=REPO_ROOT / "capabilities", workers=2
        ),
        environ=env,
    )
    with TestClient(create_app(Gate(config, environ=env), service)) as api:
        yield api, tenant, env


_ids = iter(range(1, 10_000))


def post(api: TestClient, path: str, body: Any, **headers: str) -> Any:
    return api.post(
        path,
        content=body if isinstance(body, str) else json.dumps(body),
        headers={**HEADERS, "X-Request-Id": f"it-{next(_ids)}", **headers},
    )


def logical(result: dict[str, Any]) -> dict[str, Any]:
    return {k: result.get(k) for k in LOGICAL}


def test_an_api_invocation_answers_what_the_cli_answers(
    setup: tuple[TestClient, Path, dict[str, str]], tmp_path: Path
) -> None:
    api, tenant, env = setup
    args = {"member_id": "10003"}
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "cua.cli",
            "catalog",
            "invoke",
            "member_savings_balance",
            "--args",
            json.dumps(args),
            "--tenant",
            str(tenant),
            "--runs-dir",
            str(tmp_path / "cli-runs"),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert cli.returncode == 0, cli.stderr
    by_cli = json.loads(cli.stdout)

    r = post(api, "/runs?wait=120", {"capability": "member_savings_balance", "inputs": args})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["state"] == "finished"
    by_api = run["result"]
    assert by_api["kind"] == "success" and by_api["outputs"]["savings_balance"] == "1411.21"
    assert logical(by_api) == logical(by_cli)
    assert list(by_api) == list(by_cli)  # the same fields, in the order the CLI prints them
    assert by_api["run_id"] == run["run_id"]

    events = api.get(f"/runs/{run['run_id']}/events", headers=HEADERS).json()["events"]
    types = [e["type"] for e in events]
    assert types[0] == "run.started" and "run.completed" in types
    assert {e["run_id"] for e in events} == {run["run_id"]}
    assert all(e["tenant_id"] == "local" for e in events)


def test_a_commit_is_escalated_for_consent_and_completed_over_http(
    setup: tuple[TestClient, Path, dict[str, str]],
) -> None:
    api, tenant_file, _ = setup
    body = (
        '{"capability": "open_subaccount", "inputs": {"member_id": "10003", '
        '"initial_deposit": 250.00}, "handoff": {"ttl_s": 600}, "screenshots": false}'
    )
    r = post(api, "/runs?wait=120", body, **{"Idempotency-Key": "open-1"})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["state"] == "escalated"
    assert run["result"]["reason"] == "NEEDS_APPROVAL" and run["result"]["side_effect"] == "none"
    run_dir = Path(run["result"]["evidence"]["run_dir"])
    session = json.loads((run_dir / "session.json").read_text())
    browser = BrowserProcess(session["pid"], session["cdp_url"], Path(session["profile"]))
    try:
        assert browser.alive()
        tenant = load_tenant(str(tenant_file))
        tokens.create_signing_key(tenant)
        cap = open_capability(REPO_ROOT / "capabilities" / "open_subaccount.json").capability
        key = tokens.signing_key(tenant)

        other = tokens.mint(
            cap,
            tenant,
            {"member_id": "10003", "initial_deposit": "999.00"},
            approved_by="rev",
            key=key,
        )
        refused = post(api, f"/runs/{run['run_id']}/approve", {"token": other})
        assert refused.status_code == 403, refused.text
        assert "approval refused" in refused.json()["error"]["message"]
        assert api.get(f"/runs/{run['run_id']}", headers=HEADERS).json()["state"] == "escalated"
        assert browser.alive()

        # The inputs as replay saw them: 250.00, exactly as the body sent it.
        token = tokens.mint(
            cap,
            tenant,
            {"member_id": "10003", "initial_deposit": "250.00"},
            approved_by="rev",
            key=key,
        )
        done = post(
            api, f"/runs/{run['run_id']}/approve?wait=120", {"token": token, "approved_by": "rev"}
        )
        assert done.status_code == 200, done.text
        result = done.json()["result"]
        assert done.json()["state"] == "finished"
        assert result["kind"] == "success" and result["side_effect"] == "committed"
        assert result["outputs"]["reference_number"].startswith("REF-10003-")
        assert result["handoffs"][0]["decided_by"] == "api:agent"
        assert result["idempotency_key"] == "open-1"

        retry = post(api, "/runs?wait=5", body, **{"Idempotency-Key": "open-1"})
        assert retry.headers["Idempotent-Replayed"] == "true"
        assert retry.json()["run_id"] == run["run_id"]
        assert retry.json()["result"]["outputs"] == result["outputs"]
    finally:
        if browser.alive():
            browser.kill()


def test_an_escalated_run_is_aborted_over_http(
    setup: tuple[TestClient, Path, dict[str, str]],
) -> None:
    api, _, _ = setup
    body = {
        "capability": "open_subaccount",
        "inputs": {"member_id": "10003", "initial_deposit": "10.00"},
        "handoff": {},
        "screenshots": False,
    }
    run = post(api, "/runs?wait=120", body, **{"Idempotency-Key": "open-abort"}).json()
    assert run["state"] == "escalated", run
    session = json.loads((Path(run["result"]["evidence"]["run_dir"]) / "session.json").read_text())
    browser = BrowserProcess(session["pid"], session["cdp_url"], Path(session["profile"]))
    try:
        r = post(api, f"/runs/{run['run_id']}/abort", {"why": "not today"})
        assert r.status_code == 200, r.text
        aborted = r.json()
        assert aborted["state"] == "finished"
        assert aborted["result"]["code"] == "ESCALATION_ABORTED"
        assert aborted["result"]["side_effect"] == "none"
        assert "api:agent" in aborted["result"]["message"]
        assert not browser.alive()
        again = post(api, f"/runs/{run['run_id']}/resume", {})
        assert again.status_code == 409
    finally:
        if browser.alive():
            browser.kill()


def test_an_escalated_run_nobody_answers_expires_when_it_is_read(
    setup: tuple[TestClient, Path, dict[str, str]],
) -> None:
    api, _, _ = setup
    body = {
        "capability": "open_subaccount",
        "inputs": {"member_id": "10003", "initial_deposit": "10.00"},
        "handoff": {"ttl_s": 1},
        "screenshots": False,
    }
    run = post(api, "/runs?wait=120", body, **{"Idempotency-Key": "open-expire"}).json()
    assert run["state"] == "escalated", run
    session = json.loads((Path(run["result"]["evidence"]["run_dir"]) / "session.json").read_text())
    browser = BrowserProcess(session["pid"], session["cdp_url"], Path(session["profile"]))
    try:
        time.sleep(1.5)
        expired = api.get(f"/runs/{run['run_id']}", headers=HEADERS).json()
        assert expired["state"] == "finished"
        assert expired["result"]["code"] == "ESCALATION_ABORTED"
        assert "expired" in expired["result"]["message"]
        assert not browser.alive()
    finally:
        if browser.alive():
            browser.kill()
