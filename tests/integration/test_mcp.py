"""An MCP client's view, end to end: ``cua mcp`` as its own process on stdio,
against the mock app.

The acceptance line for MCP: an agent discovers the approved capabilities
and invokes them knowing nothing of the GUI. It looks a balance up, opens a
sub-account, is told consent is needed and which tool carries the run on,
and completes the commit with a token, all through tool calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from cua.artifact.store import open_capability
from cua.policy import tokens
from cua.surface.playwright_surface import BrowserProcess
from cua.tenant import load_tenant
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.browser


class Client:
    """The smallest MCP client: one request, one answer, in order."""

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.proc = proc
        self.next_id = 0

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        answer: dict[str, Any] = json.loads(self.proc.stdout.readline())
        assert answer["id"] == self.next_id, answer
        return answer

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        result: dict[str, Any] = self.request("tools/call", {"name": name, "arguments": arguments})[
            "result"
        ]
        return result


@pytest.fixture
def mcp(mockapp_url: str, tmp_path: Path) -> Iterator[tuple[Client, Path]]:
    tenant = tmp_path / "tenant.yaml"
    tenant.write_text(
        (REPO_ROOT / "tenants" / "local.yaml")
        .read_text()
        .replace("http://127.0.0.1:8000", mockapp_url)
        .replace(".cua/approval-signing.key", (tmp_path / "signing.key").as_posix())
    )
    access = tmp_path / "access.yaml"
    access.write_text(
        yaml.safe_dump(
            {
                "tenants": {
                    "local": {
                        "file": str(tenant),
                        "policy": str(REPO_ROOT / "policies" / "default.yaml"),
                    }
                },
                "clients": {
                    "agent": {
                        "key": {"provider": "env", "var": "UNUSED_OVER_STDIO"},
                        "tenants": ["local"],
                        "capabilities": ["*"],
                        "scopes": ["read", "invoke", "approve", "operate"],
                    }
                },
            }
        )
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cua.cli",
            "mcp",
            "--client",
            "agent",
            "--access",
            str(access),
            "--runs-dir",
            str(tmp_path / "runs"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
        env={**os.environ, "CUA_SECRET_MOCKCORE_OPERATOR": "operator:operator"},
    )
    client = Client(proc)
    try:
        init = client.request("initialize", {"protocolVersion": "2025-06-18"})
        assert init["result"]["serverInfo"]["name"] == "inter-cua"
        yield client, tenant
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=60)


def test_an_agent_discovers_and_invokes_capabilities_knowing_nothing_of_the_gui(
    mcp: tuple[Client, Path],
) -> None:
    client, tenant_file = mcp
    listed = client.request("tools/list")["result"]["tools"]
    assert {"member_savings_balance", "open_subaccount"} <= {t["name"] for t in listed}

    balance = client.call("member_savings_balance", member_id="10003")
    assert balance["isError"] is False
    view = balance["structuredContent"]
    assert view["kind"] == "success" and view["outputs"]["savings_balance"] == "1411.21"
    assert "locator_rungs_used" not in view and "evidence" not in view

    unknown = client.call("member_savings_balance", member_id="99999")
    assert unknown["isError"] is False
    assert unknown["structuredContent"]["kind"] == "business_outcome"
    assert unknown["structuredContent"]["code"] == "NOT_FOUND"

    stray = client.call("member_savings_balance", member_id="10003", colour="red")
    assert stray["isError"] is True and stray["structuredContent"]["code"] == "INPUT_INVALID"

    opened = client.call(
        "open_subaccount", member_id="10003", initial_deposit="250.00", idempotency_key="mcp-1"
    )
    escalated = opened["structuredContent"]
    assert escalated.get("kind") == "escalated", escalated
    assert escalated["reason"] == "NEEDS_APPROVAL"
    assert escalated["side_effect"] == "none" and escalated["resume_token"]
    assert "cua_approve_run" in escalated["required_action"]

    session = json.loads(
        (Path(mcp[1]).parent / "runs" / escalated["run_id"] / "session.json").read_text()
    )
    browser = BrowserProcess(session["pid"], session["cdp_url"], Path(session["profile"]))
    try:
        tenant = load_tenant(str(tenant_file))
        tokens.create_signing_key(tenant)
        cap = open_capability(REPO_ROOT / "capabilities" / "open_subaccount.json").capability
        token = tokens.mint(
            cap,
            tenant,
            {"member_id": "10003", "initial_deposit": "250.00"},
            approved_by="reviewer",
            key=tokens.signing_key(tenant),
        )
        done = client.call("cua_approve_run", run_id=escalated["run_id"], approval_token=token)
        result = done["structuredContent"]
        assert done["isError"] is False, result
        assert result["kind"] == "success" and result["side_effect"] == "committed"
        assert result["outputs"]["reference_number"].startswith("REF-10003-")

        again = client.call(
            "open_subaccount",
            member_id="10003",
            initial_deposit="250.00",
            idempotency_key="mcp-1",
        )
        assert again["structuredContent"]["run_id"] == escalated["run_id"]
        assert again["structuredContent"]["outputs"] == result["outputs"]
    finally:
        if browser.alive():
            browser.kill()
