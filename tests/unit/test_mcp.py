"""The MCP server without a browser: the protocol, the tool list an agent
sees, and tool calls through the run service with the runner faked
(``tests.unit.test_api.FakeRunner``). ``tests/integration/test_mcp.py`` drives
the real ``cua mcp`` process against the mock app."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cua.api.access import Caller, Gate
from cua.api.models import ApiRun
from cua.api.service import RunService, ServiceSettings
from cua.mcp import tools
from cua.mcp.server import McpServer
from tests.unit.test_api import AGENT_KEY, READER_KEY, REPO, FakeRunner, access

ENV = {"AGENT_KEY": AGENT_KEY, "READER_KEY": READER_KEY}


def make_server(runs: Path, fake: FakeRunner, client: str = "agent", **options: Any) -> McpServer:
    config = access()
    gate = Gate(config, environ=ENV)
    tenant, policy = gate.tenants["local"]
    service = RunService(
        ServiceSettings(runs_dir=runs, capabilities_dir=REPO / "capabilities"),
        replay=fake.replay,
        resume=fake.resume,
        check_resume=fake.check_resume,
    )
    return McpServer(
        service,
        lambda rid: Caller(client, config.clients[client], tenant, policy, rid),
        wait_s=10,
        **options,
    )


@pytest.fixture
def fake() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def server(tmp_path: Path, fake: FakeRunner) -> Iterator[McpServer]:
    s = make_server(tmp_path / "runs", fake)
    yield s
    s.service.close()


_ids = iter(range(1, 10_000))


def rpc(server: McpServer, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": next(_ids), "method": method}
    if params is not None:
        message["params"] = params
    response = server.handle(message)
    assert response is not None and response["id"] == message["id"]
    return response


def call(server: McpServer, name: str, **arguments: Any) -> dict[str, Any]:
    response = rpc(server, "tools/call", {"name": name, "arguments": arguments})
    assert "result" in response, response
    result: dict[str, Any] = response["result"]
    return result


# -- the protocol ------------------------------------------------------------------


def test_initialize_negotiates_the_protocol_version(server: McpServer) -> None:
    result = rpc(server, "initialize", {"protocolVersion": "2025-03-26"})["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "inter-cua" and "escalated" in result["instructions"]
    newest = rpc(server, "initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert newest["protocolVersion"] == "2025-06-18"


def test_protocol_errors_are_for_what_the_client_got_wrong(server: McpServer) -> None:
    assert rpc(server, "ping")["result"] == {}
    assert rpc(server, "resources/list")["error"]["code"] == -32601
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle([1, 2])["error"]["code"] == -32600  # type: ignore[index]
    assert server.handle({"id": 1, "method": "ping"})["error"]["code"] == -32600  # type: ignore[index]
    unknown = rpc(server, "tools/call", {"name": "click", "arguments": {"ref": "e1"}})
    assert unknown["error"]["code"] == -32602 and "unknown tool" in unknown["error"]["message"]


def test_serve_speaks_one_json_message_per_line(server: McpServer) -> None:
    lines = [
        "not json\n",
        "\n",
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n",
    ]
    out = io.StringIO()
    server.serve(io.StringIO("".join(lines)), out)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert answers[0]["error"]["code"] == -32700 and answers[0]["id"] is None
    assert answers[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert len(answers) == 2


def test_a_long_tool_call_does_not_hold_up_the_session(server: McpServer, fake: FakeRunner) -> None:
    fake.gate = threading.Event()
    lines = [
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "slow",
                "method": "tools/call",
                "params": {"name": "member_savings_balance", "arguments": {"member_id": "10003"}},
            }
        ),
        json.dumps({"jsonrpc": "2.0", "id": "quick", "method": "ping"}),
    ]
    threading.Timer(0.5, fake.gate.set).start()
    out = io.StringIO()
    server.serve(io.StringIO("\n".join(lines) + "\n"), out)
    order = [json.loads(line)["id"] for line in out.getvalue().splitlines()]
    assert order == ["quick", "slow"]


# -- the tools an agent sees -------------------------------------------------------


def test_each_approved_capability_is_a_tool_and_no_browser_primitive_is(
    server: McpServer,
) -> None:
    listed = rpc(server, "tools/list")["result"]["tools"]
    names = [t["name"] for t in listed]
    assert names == [
        "member_savings_balance",
        "open_subaccount",
        "cua_run_status",
        "cua_approve_run",
        "cua_resume_run",
        "cua_abort_run",
    ]
    assert not {"click", "type", "press", "navigate", "read"} & set(names)
    for tool in listed:
        text = json.dumps(tool)
        assert "ref" not in tool["inputSchema"]["properties"]
        assert "secret://" not in text and "role_name" not in text and "near_text" not in text


def test_a_capability_tool_says_what_it_does_and_what_it_needs(server: McpServer) -> None:
    listed = {t["name"]: t for t in rpc(server, "tools/list")["result"]["tools"]}
    lookup, open_ = listed["member_savings_balance"], listed["open_subaccount"]
    assert lookup["inputSchema"]["required"] == ["member_id"]
    assert lookup["annotations"]["readOnlyHint"] and lookup["annotations"]["idempotentHint"]
    assert "Read only" in lookup["description"] and "savings_balance" in lookup["description"]
    schema = open_["inputSchema"]
    assert schema["required"] == ["member_id", "initial_deposit", "idempotency_key"]
    assert "approval_token" in schema["properties"] and schema["additionalProperties"] is False
    text = open_["description"]
    for promise in ("reference_number", "Side effect", "Not idempotent", "NEEDS_APPROVAL"):
        assert promise in text
    assert "cua_approve_run" in text and "cua resume" not in text
    assert open_["annotations"]["destructiveHint"] and not open_["annotations"]["idempotentHint"]


def test_a_client_sees_only_what_it_may_use(tmp_path: Path, fake: FakeRunner) -> None:
    reader = make_server(tmp_path / "runs", fake, client="reader")
    listed = rpc(reader, "tools/list")["result"]["tools"]
    assert [t["name"] for t in listed] == ["member_savings_balance", "cua_run_status"]
    refused = rpc(reader, "tools/call", {"name": "open_subaccount", "arguments": {}})
    assert refused["error"]["code"] == -32602
    reader.service.close()


# -- calling -------------------------------------------------------------------------


def test_a_call_answers_with_the_result_an_agent_acts_on(
    server: McpServer, fake: FakeRunner
) -> None:
    result = call(server, "member_savings_balance", member_id="10003")
    assert result["isError"] is False
    view = result["structuredContent"]
    assert view["state"] == "finished" and view["kind"] == "success"
    assert view["outputs"] == {"savings_balance": "1411.21"}
    assert result["content"][0]["text"].startswith("success:")
    assert json.loads(result["content"][1]["text"]) == view
    [invoked] = fake.calls
    assert invoked["handoff"] is None and invoked["run_id"] == view["run_id"]
    assert set(view) <= {"run_id", "state", "required_action", "error", *tools.AGENT_FIELDS}


def test_how_the_gui_was_driven_stays_in_the_run_directory() -> None:
    record = ApiRun(
        run_id="run_X",
        state="finished",
        capability="member_savings_balance",
        tenant="local",
        client="agent",
        request_id="r",
        created_at="t",
        updated_at="t",
        result={
            "kind": "failure",
            "code": "LOCATOR_UNRESOLVED",
            "step_id": "search.submit",
            "expected": "button 'Search'",
            "observed": "[e7] button 'Find' ... member 123-45-6789",
            "message": "no rung named the control",
            "side_effect": "none",
            "locator_rungs_used": {"login.submit": "role_name"},
            "evidence": {"run_dir": "evidence/runs/run_X", "screenshots": ["0001.png"]},
            "recoveries": [],
        },
    )
    view = tools.agent_view(record)
    assert view["code"] == "LOCATOR_UNRESOLVED" and view["message"]
    for hidden in ("expected", "observed", "locator_rungs_used", "evidence", "recoveries"):
        assert hidden not in view
    assert "123-45-6789" not in json.dumps(view)


def test_numbers_reach_the_capability_as_they_were_sent(
    server: McpServer, fake: FakeRunner
) -> None:
    line = (
        '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": '
        '"open_subaccount", "arguments": {"member_id": "10003", "initial_deposit": 250.00, '
        '"idempotency_key": "d1"}}}\n'
    )
    server.serve(io.StringIO(line), io.StringIO())
    assert fake.calls[0]["invocation"].inputs["initial_deposit"] == "250.00"


def test_a_refusal_is_a_tool_error_the_model_can_read(server: McpServer, fake: FakeRunner) -> None:
    missing = call(server, "open_subaccount", member_id="10003", initial_deposit=250)
    assert missing["isError"] is True
    assert missing["structuredContent"]["error"]["code"] == "idempotency_key_required"
    typed = call(server, "member_savings_balance", member_id=10003)
    assert typed["isError"] is True
    assert typed["structuredContent"]["kind"] == "failure"
    assert typed["structuredContent"]["code"] == "INPUT_INVALID"
    assert fake.calls == []


def test_escalated_says_what_has_to_happen_and_which_tool_carries_it_on(
    server: McpServer, fake: FakeRunner
) -> None:
    fake.answer = "escalated"
    result = call(
        server, "open_subaccount", member_id="10003", initial_deposit=250, idempotency_key="k1"
    )
    view = result["structuredContent"]
    assert result["isError"] is False and view["state"] == "escalated"
    assert view["reason"] == "NEEDS_APPROVAL" and view["resume_token"] == "rt_secret"
    assert "cua_approve_run" in view["required_action"]
    assert fake.calls[-1]["handoff"] is not None  # a capability that may ask for consent

    refused = call(server, "cua_approve_run", run_id=view["run_id"], approval_token="bad")
    assert refused["isError"] and refused["structuredContent"]["error"]["code"] == (
        "approval_refused"
    )
    status = call(server, "cua_run_status", run_id=view["run_id"])["structuredContent"]
    assert status["state"] == "escalated" and "approval refused" in status["error"]

    done = call(server, "cua_approve_run", run_id=view["run_id"], approval_token="good")
    assert done["structuredContent"]["kind"] == "success"
    assert done["structuredContent"]["side_effect"] == "committed"
    assert fake.resumes[0]["by"] == "api:agent"


def test_a_token_rides_on_the_capability_tool_too(server: McpServer, fake: FakeRunner) -> None:
    call(
        server,
        "open_subaccount",
        member_id="10003",
        initial_deposit=250,
        idempotency_key="k2",
        approval_token="cat1.x.y",
    )
    assert fake.calls[0]["invocation"].approval.token == "cat1.x.y"


def test_handoff_follows_the_servers_setting(tmp_path: Path, fake: FakeRunner) -> None:
    for setting, lookup, commit in (("none", False, False), ("all", True, True)):
        s = make_server(tmp_path / setting, fake, handoff=setting)
        call(s, "member_savings_balance", member_id="10003")
        call(s, "open_subaccount", member_id="1", initial_deposit=1, idempotency_key=setting)
        assert (fake.calls[-2]["handoff"] is not None) is lookup
        assert (fake.calls[-1]["handoff"] is not None) is commit
        s.service.close()


def test_run_tools_are_checked_like_the_api(server: McpServer, fake: FakeRunner) -> None:
    missing = call(server, "cua_run_status")
    assert missing["isError"] and "run_id" in missing["structuredContent"]["error"]["message"]
    nowhere = call(server, "cua_run_status", run_id="run_NOPE")
    assert nowhere["structuredContent"]["error"]["code"] == "run_not_found"
    run_id = call(server, "member_savings_balance", member_id="10003")["structuredContent"][
        "run_id"
    ]
    extra = call(server, "cua_run_status", run_id=run_id, colour="red")
    assert extra["isError"] and "colour" in extra["structuredContent"]["error"]["message"]
    finished = call(server, "cua_abort_run", run_id=run_id)
    assert finished["structuredContent"]["error"]["code"] == "run_not_escalated"


def test_every_call_is_on_record(server: McpServer, tmp_path: Path) -> None:
    call(server, "member_savings_balance", member_id="10003")
    line = json.loads((tmp_path / "runs" / ".api" / "requests.jsonl").read_text().splitlines()[-1])
    assert line["method"] == "MCP" and line["path"] == "tools/call member_savings_balance"
    assert line["client"] == "agent" and line["status"] == "ok"
    assert line["request_id"].startswith("mcp_")


# -- the command ------------------------------------------------------------------------


def test_cua_mcp_answers_on_stdout_and_says_everything_else_on_stderr(tmp_path: Path) -> None:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "x"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "cua.cli",
            "mcp",
            "--root",
            str(REPO),
            "--runs-dir",
            str(tmp_path / "runs"),
        ],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,  # started elsewhere, as an MCP client may; --root finds the repository
        timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert out.returncode == 0, out.stderr
    answers = [json.loads(line) for line in out.stdout.splitlines()]
    assert [a["id"] for a in answers] == [1, 2]
    assert [t["name"] for t in answers[1]["result"]["tools"]][:2] == [
        "member_savings_balance",
        "open_subaccount",
    ]
    assert "serving tenant local as client local-agent" in out.stderr


def test_cua_mcp_refuses_a_client_or_tenant_it_does_not_know(tmp_path: Path) -> None:
    for argv in (["--client", "nobody"], ["--client", "balance-reader", "--tenant", "desk"]):
        out = subprocess.run(
            [sys.executable, "-m", "cua.cli", "mcp", *argv],
            input="",
            capture_output=True,
            text=True,
            cwd=REPO,
            timeout=60,
        )
        assert out.returncode == 64 and out.stdout == ""


def test_the_mcp_server_imports_no_model_client() -> None:
    code = (
        "import sys\n"
        "import cua.mcp.server, cua.mcp.cli, cua.mcp.tools\n"
        "bad = [m for m in sys.modules if m == 'anthropic' or m.startswith(('anthropic.',"
        " 'google.genai', 'cua.agent'))]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=REPO
    )
    assert out.stdout.strip() == ""
