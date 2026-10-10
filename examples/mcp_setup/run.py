"""Execute this directory's machine-readable scenario through ordinary entry points."""

from __future__ import annotations

import json
from contextlib import ExitStack
from typing import Any

from examples._session import Session
from examples._support import check_subset, require
from examples.mcp_setup.client import StdioClient, configuration


def run(s: Session, stack: ExitStack) -> dict[str, Any]:
    config = configuration(s.work)
    (s.out / "mcp-config.json").write_text(json.dumps(config, indent=2) + "\n")
    client = StdioClient(config, cwd=s.cwd, env=s.env)
    try:
        before = s.oracle()
        init = client.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "example", "version": "1"},
            },
        )
        require(init["serverInfo"]["name"] == "inter-cua", "unexpected MCP server")
        client.notify("notifications/initialized")
        tools = client.request("tools/list")["tools"]
        names = {tool["name"] for tool in tools}
        require(set(s.spec["capabilities"]) <= names, "approved tools not listed")
        result = client.request(
            "tools/call", {"name": "item_lookup", "arguments": s.spec["inputs"]}
        )
        check_subset(
            result,
            {
                "isError": False,
                "structuredContent": {"kind": "success", "outputs": s.spec["outputs"]},
            },
        )
        require(s.oracle() == before, "MCP lookup changed state")
        runs = list((s.work / "evidence/runs").glob("run_*/result.json"))
        require(bool(runs), "MCP result evidence missing")
        for path in runs:
            data = json.loads(path.read_text())
            if data["kind"] == "success":
                s.no_models(data)
        s.record(
            ["MCP", "initialize; tools/list; tools/call item_lookup"],
            0,
            result["structuredContent"],
        )
        return {
            "unrelated_cwd": True,
            "approved_tool_listed": True,
            "kind": "success",
            "replay_model_calls": 0,
        }
    finally:
        client.close()
