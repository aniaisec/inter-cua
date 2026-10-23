"""Operator HTTP boundary and durable assignment, independent of browser control."""

from __future__ import annotations

import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cua.escalation.access import OperatorConfig, OperatorGate
from cua.escalation.controller import ControlStore
from cua.escalation.operator_app import Console, create_app
from cua.persistence import atomic_write_json, read_jsonl
from cua.replay import runner
from tests.operator_support import OPERATOR_KEY, operator_gate
from tests.unit.test_escalation import REQUEST, channel


def setup_console(tmp_path: Path) -> tuple[TestClient, str]:
    ch = channel(tmp_path)
    ticket = ch.open(REQUEST)
    app = create_app(tmp_path / "runs", tenant="local", gate=operator_gate(tmp_path))
    # Capture is covered by browser tests; avoid attempting CDP in these boundary tests.
    app.state.console._start_capture = lambda *args: None
    client = TestClient(app)
    client.auth = ("ann", OPERATOR_KEY)
    client.headers["X-Cua-Operator-Request"] = "1"
    return client, ticket.request_id


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/requests/req_x",
        "/requests/req_x/screenshot",
        "/api/requests",
        "/api/requests/req_x",
        "/api/requests/req_x/history",
        "/api/requests/req_x/session",
        "/health",
        "/ready",
        "/docs",
        "/openapi.json",
    ],
)
def test_every_surface_requires_verified_identity(tmp_path: Path, path: str) -> None:
    client, _ = setup_console(tmp_path)
    client.auth = None
    assert client.get(path).status_code == 401
    assert client.post(path, json={}).status_code == 401
    client.auth = ("ann", "wrong")
    assert client.get(path).status_code == 401


def test_roles_tenant_grants_and_spoofed_names(tmp_path: Path) -> None:
    client, rid = setup_console(tmp_path)
    base = f"/api/requests/{rid}"
    client.auth = ("beta", OPERATOR_KEY + "b")
    assert client.get("/api/requests").status_code == 401
    client.auth = ("reader", OPERATOR_KEY + "r")
    assert client.get(base).status_code == 200
    for action in ("take_control", "resume", "approve", "abort"):
        assert client.post(f"{base}/{action}", json={}).status_code == 403
    assert client.get(f"{base}/session").status_code == 403
    client.auth = ("worker", OPERATOR_KEY + "w")
    assert client.post(f"{base}/approve", json={}).status_code == 403
    client.auth = ("ann", OPERATOR_KEY)
    assert client.post(f"{base}/approve", json={"by": "someone_else"}).status_code == 422
    response = client.post(f"{base}/approve", json={})
    assert response.status_code == 200
    assert response.json()["decision"]["by"] == "ann"
    assert response.json()["assigned_to"] == "ann"
    history = client.get(f"{base}/history").json()
    assert history[-1]["by"] == "ann" and history[-1]["assignee"] == "ann"


def test_tenant_console_hides_other_tenants_request_and_screenshot(tmp_path: Path) -> None:
    _, rid = setup_console(tmp_path)
    with TestClient(
        create_app(tmp_path / "runs", tenant="beta", gate=operator_gate(tmp_path))
    ) as other:
        other.auth = ("beta", OPERATOR_KEY + "b")
        assert other.get("/api/requests").json() == []
        for path in (
            f"/requests/{rid}",
            f"/requests/{rid}/screenshot",
            f"/api/requests/{rid}",
            f"/api/requests/{rid}/history",
        ):
            assert other.get(path).status_code == 404


def test_json_and_browser_forms_require_csrf_protection(tmp_path: Path) -> None:
    client, rid = setup_console(tmp_path)
    base = f"/api/requests/{rid}"
    del client.headers["X-Cua-Operator-Request"]
    assert client.post(f"{base}/approve", json={}).status_code == 403
    client.headers["X-Cua-Operator-Request"] = "1"
    assert (
        client.post(f"{base}/approve", json={}, headers={"Origin": "https://evil.test"}).status_code
        == 403
    )
    assert (
        client.post(
            f"{base}/approve", json={}, headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )
    assert client.post(f"/requests/{rid}/approve", data={}).status_code == 403
    page = client.get(f"/requests/{rid}")
    assert page.headers["cache-control"] == "no-store"
    assert "name=by" not in page.text and "localStorage" not in page.text
    assert "http://127.0.0.1:9" not in page.text
    match = re.search(r'action="[^"]+/approve"[^>]*><input[^>]+value="([^"]+)"', page.text)
    assert match is not None
    token = match[1]
    assert client.post(f"/requests/{rid}/resume", data={"csrf": token}).status_code == 403
    client.auth = ("pat", OPERATOR_KEY + "p")
    assert client.post(f"/requests/{rid}/approve", data={"csrf": token}).status_code == 403
    client.auth = ("ann", OPERATOR_KEY)
    assert client.post(f"/requests/{rid}/approve", data={"csrf": token}).status_code == 200
    assert ControlStore(tmp_path / "runs/run_1").read().decision.by == "ann"


def test_assignment_survives_restart_and_blocks_other_operators(tmp_path: Path) -> None:
    client, rid = setup_console(tmp_path)
    base = f"/api/requests/{rid}"
    assert "cdp_url" not in client.get(base).json()["request"]
    assert client.get(f"{base}/session").status_code == 409
    assert client.post(f"{base}/take_control", json={}).status_code == 200
    assert client.get(f"{base}/session").json()["cdp_url"] == "http://127.0.0.1:9"
    with TestClient(
        create_app(tmp_path / "runs", tenant="local", gate=operator_gate(tmp_path))
    ) as fresh:
        fresh.auth = ("pat", OPERATOR_KEY + "p")
        fresh.headers["X-Cua-Operator-Request"] = "1"
        assert fresh.get(base).json()["assigned_to"] == "ann"
        for action in ("take_control", "resume", "abort", "approve"):
            assert fresh.post(f"{base}/{action}", json={}).status_code == 409
        assert fresh.get(f"{base}/session").status_code == 409
    assert client.post(f"{base}/resume", json={}).status_code == 200


def test_competing_process_equivalent_consoles_cannot_both_acquire(tmp_path: Path) -> None:
    _, rid = setup_console(tmp_path)

    def acquire(name: str) -> bool:
        console = Console(tmp_path / "runs", tenant="local")
        console._start_capture = lambda *args: None
        from cua.escalation.operator_app import ConsoleError

        try:
            console.act(rid, "take_control", by=name)
            return True
        except ConsoleError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(acquire, ("ann", "pat"))) == 1


def test_expired_request_cannot_be_claimed_or_approved(tmp_path: Path) -> None:
    ch = channel(tmp_path)
    ticket = ch.open(REQUEST)
    from cua.escalation.requests import Queue

    queue = Queue(tmp_path / "runs")
    entry = queue.by_token(ticket.resume_token)
    request = queue.request(entry).model_dump(mode="json")
    request["expires_at"] = "2000-01-01T00:00:00Z"
    atomic_write_json(ch.log.dir / "interventions" / f"{ticket.request_id}.json", request)
    with TestClient(
        create_app(tmp_path / "runs", tenant="local", gate=operator_gate(tmp_path))
    ) as client:
        client.auth = ("ann", OPERATOR_KEY)
        client.headers["X-Cua-Operator-Request"] = "1"
        for action in ("take_control", "approve", "resume"):
            assert (
                client.post(f"/api/requests/{ticket.request_id}/{action}", json={}).status_code
                == 409
            )
        assert ch.control.read().state == "PAUSED"


def test_readiness_and_assignment_corruption_fail_closed(tmp_path: Path) -> None:
    client, _ = setup_console(tmp_path)
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").status_code == 200
    path = tmp_path / "runs/run_1/control.json"
    data = ControlStore(path.parent).read().model_dump(mode="json")
    data["assignee"] = "forged"
    atomic_write_json(path, data)
    assert client.get("/ready").status_code == 503
    assert client.get("/api/requests").status_code == 409


def test_bad_operator_keys_fail_at_startup(tmp_path: Path) -> None:
    config = OperatorConfig.model_validate(
        {
            "operators": {
                "ann": {"key": {"provider": "env", "var": "KEY"}, "tenants": {"local": ["read"]}}
            }
        }
    )
    with pytest.raises(ValueError, match="32"):
        OperatorGate(config, root=tmp_path, environ={"KEY": "short"})


def test_csrf_tokens_expire_and_are_invalid_after_restart(tmp_path: Path, monkeypatch) -> None:
    gate = operator_gate(tmp_path)
    from cua.escalation.access import Principal

    actor = Principal("ann", "local", frozenset({"read", "operate"}))
    monkeypatch.setattr("cua.escalation.access.time.time", lambda: 1000)
    token = gate.csrf(actor, "req_1", "resume")
    assert gate.check_csrf(token, actor, "req_1", "resume")
    assert not operator_gate(tmp_path).check_csrf(token, actor, "req_1", "resume")
    assert not gate.check_csrf("1000.non-ascii-\u00e9", actor, "req_1", "resume")
    monkeypatch.setattr("cua.escalation.access.time.time", lambda: 2000)
    assert not gate.check_csrf(token, actor, "req_1", "resume")


def test_competing_processes_have_one_durable_assignee(tmp_path: Path) -> None:
    _, rid = setup_console(tmp_path)
    script = """
import sys
from pathlib import Path
from cua.escalation.operator_app import Console, ConsoleError
console = Console(Path(sys.argv[1]), tenant="local")
console._start_capture = lambda *args: None
try:
    console.act(sys.argv[2], "take_control", by=sys.argv[3])
    print("claimed")
except ConsoleError:
    print("refused")
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path / "runs"), rid, name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for name in ("ann", "pat")
    ]
    outputs = [process.communicate(timeout=30) for process in processes]
    assert sorted(out.strip() for out, err in outputs) == ["claimed", "refused"], outputs
    assert all(process.returncode == 0 for process in processes)
    assert ControlStore(tmp_path / "runs/run_1").read().assignee in ("ann", "pat")


def test_resume_token_cannot_steal_a_human_control_interval(tmp_path: Path, monkeypatch) -> None:
    ch = channel(tmp_path)
    ticket = ch.open(REQUEST)
    ch.control.transition("HUMAN_IN_CONTROL", by="ann", expect_request=ticket.request_id)
    monkeypatch.setattr(runner, "read_model", lambda *args: SimpleNamespace())
    with pytest.raises(runner.InvocationError, match="operator holds"):
        runner.check_resume(ticket.resume_token, runs_dir=tmp_path / "runs")
    assert ch.control.read().assignee == "ann"
    assert ch.control.read().state == "HUMAN_IN_CONTROL"


def test_operator_cli_refuses_invalid_access_before_starting(tmp_path: Path, monkeypatch) -> None:
    from cua.cli import main

    def no_server(*args, **kwargs):
        pytest.fail("invalid operator access started a server")

    monkeypatch.setattr("uvicorn.run", no_server)
    malformed = tmp_path / "access.yaml"
    malformed.write_text("operators: [unterminated", encoding="utf-8")
    assert main(["operator", "--operator-access", str(malformed)]) == 64
    assert main(["operator", "--operator-access", str(tmp_path / "missing.yaml")]) == 64


def test_captured_browser_actions_use_the_assigned_identity(tmp_path: Path, monkeypatch) -> None:
    ch = channel(tmp_path)
    ticket = ch.open(REQUEST)

    class Capture:
        def __init__(self, *, sink, **kwargs):
            self.sink = sink

        def start(self):
            self.sink({"action": "click", "by": "page-supplied-spoof"})

        def stop(self):
            pass

    monkeypatch.setattr("cua.escalation.operator_app.CaptureSession", Capture)
    console = Console(tmp_path / "runs", tenant="local")
    console.act(ticket.request_id, "take_control", by="ann")
    actions = read_jsonl(ch.log.dir / "human_actions.jsonl")
    assert actions[-1]["source"] == "browser" and actions[-1]["by"] == "ann"
    console.close()
