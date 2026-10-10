"""Execute this directory's machine-readable scenario through ordinary entry points."""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any

import httpx

from cua.benchmark.environment import free_port
from cua.demo.runner import server
from examples._session import Session
from examples._support import ExampleError, check_subset, require
from examples.http_client.client import Conflict, RunClient, Submission


def run(s: Session, stack: ExitStack) -> dict[str, Any]:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    stack.enter_context(
        server(
            ["cua.cli", "--root", str(s.work), "serve", "--port", str(port)],
            url + "/health",
            s.cwd,
            s.env,
        )
    )
    api_key = (s.work / ".cua/api-keys/local-agent.key").read_text().strip()
    before = s.oracle()
    token = s.token("adjust_stock", s.spec["inputs"])
    submission = Submission.create(
        {"capability": "adjust_stock", "inputs": s.spec["inputs"], "approval": {"token": token}},
        key="http-example-write",
        request_id="http-example-request",
    )
    with httpx.Client(base_url=url, timeout=10) as http:
        client = RunClient(http, api_key=api_key, tenant="local")
        done = client.poll(client.submit(submission))
        check_subset(
            done, {"state": "finished", "result": {"kind": "success", "side_effect": "committed"}}
        )
        again = client.poll(client.submit(submission))
        require(again["run_id"] == done["run_id"], "HTTP retry created another run")
        try:
            client.submit(
                Submission.create(
                    {"capability": "adjust_stock", "inputs": s.spec["wrong_inputs"]},
                    key=submission.idempotency_key,
                    request_id="conflicting-request",
                )
            )
        except Conflict:
            pass
        else:
            raise ExampleError("HTTP conflict was accepted")
    s.no_models(done["result"])
    after = s.oracle()
    require(
        after["commit_count"] == before["commit_count"] + 1
        and after["commit_posts"] == before["commit_posts"] + 1,
        "HTTP repeated a commit",
    )
    s.record(
        ["HTTP", "POST /runs; GET /runs/{id}; same-key retry; conflicting retry"], 0, done["result"]
    )
    return {
        "state": done["state"],
        "commits": 1,
        "same_run_on_retry": True,
        "conflict_refused": True,
    }
