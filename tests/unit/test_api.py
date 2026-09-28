"""The HTTP API without a browser: identity, authorization, idempotency, the
run lifecycle and its records, with the runner replaced by fakes that record
what they were asked to do. ``tests/integration/test_api.py`` runs the real
runner against the mock app."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cua.api.access import AccessConfig, Gate, create_keys, load_access
from cua.api.app import create_app
from cua.api.models import ApiRun
from cua.api.service import RunService, ServiceSettings
from cua.replay.invocation import Invocation
from cua.replay.result import Escalated, Failure, ReplayResult, Success
from cua.replay.runner import InvocationError

REPO = Path(__file__).resolve().parents[2]
AGENT_KEY = "agent-key-" + "a" * 32
READER_KEY = "reader-key-" + "b" * 32
AGENT = {"Authorization": f"Bearer {AGENT_KEY}", "X-Cua-Tenant": "local"}
READER = {"Authorization": f"Bearer {READER_KEY}", "X-Cua-Tenant": "local"}
BALANCE = {"capability": "member_savings_balance", "inputs": {"member_id": "10003"}}
OPEN = {"capability": "open_subaccount", "inputs": {"member_id": "10003", "initial_deposit": 250}}


def access(**changes: Any) -> AccessConfig:
    data: dict[str, Any] = {
        "tenants": {
            "local": {
                "file": str(REPO / "tenants" / "local.yaml"),
                "policy": str(REPO / "policies" / "default.yaml"),
            },
            "desk": {
                "file": str(REPO / "tenants" / "desk.yaml"),
                "policy": str(REPO / "policies" / "deskcalc.yaml"),
            },
        },
        "clients": {
            "agent": {
                "key": {"provider": "env", "var": "AGENT_KEY"},
                "tenants": ["local", "desk"],
                "capabilities": ["*"],
                "scopes": ["read", "invoke", "approve", "operate"],
            },
            "reader": {
                "key": {"provider": "env", "var": "READER_KEY"},
                "tenants": ["local"],
                "capabilities": ["member_savings_balance"],
                "scopes": ["read", "invoke"],
            },
        },
    }
    data.update(changes)
    return AccessConfig.model_validate(data)


class FakeRunner:
    """Stands in for ``cua.replay.runner``: answers with ``answer`` and keeps
    every call. ``gate`` holds a run until the test lets it go."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.resumes: list[dict[str, Any]] = []
        self.answer: str = "success"
        self.gate: threading.Event | None = None

    def replay(self, path: Path, **kw: Any) -> ReplayResult:
        self.calls.append({"path": path, **kw})
        if self.gate is not None:
            self.gate.wait(10)
        inv: Invocation = kw["invocation"]
        common = {
            "run_id": kw["run_id"],
            "capability": path.parent.name,
            "capability_version": 3,
            "idempotency_key": inv.idempotency_key,
        }
        if self.answer == "escalated":
            return Escalated(
                reason="NEEDS_APPROVAL",
                step_id="review.submit",
                request_id="req_1",
                resume_token="rt_secret",
                **common,
            )
        if self.answer == "broken":
            raise InvocationError("credential operator: $CUA_SECRET_X is not set")
        return Success(outputs={"savings_balance": "1411.21"}, **common)

    def check_resume(self, token: str, **kw: Any) -> ReplayResult | None:
        approval = kw.get("approval")
        if approval is not None and approval.token == "bad":
            raise InvocationError("approval refused: the token grants consent for other inputs")
        return None

    def resume(self, token: str, **kw: Any) -> ReplayResult:
        self.resumes.append({"token": token, **kw})
        return Success(
            outputs={"reference_number": "REF-10003-1"},
            side_effect="committed",
            capability="open_subaccount",
            capability_version=3,
            idempotency_key="api:local:agent:k1",
        )


@pytest.fixture
def fake() -> FakeRunner:
    return FakeRunner()


def make_app(runs: Path, fake: FakeRunner, **settings: Any) -> TestClient:
    gate = Gate(access(), environ={"AGENT_KEY": AGENT_KEY, "READER_KEY": READER_KEY})
    service = RunService(
        ServiceSettings(runs_dir=runs, capabilities_dir=REPO / "capabilities", **settings),
        replay=fake.replay,
        resume=fake.resume,
        check_resume=fake.check_resume,
    )
    return TestClient(create_app(gate, service))


@pytest.fixture
def api(tmp_path: Path, fake: FakeRunner) -> Iterator[TestClient]:
    with make_app(tmp_path / "runs", fake) as client:
        yield client


_ids = iter(range(1, 10_000))


def post(api: TestClient, path: str, body: Any, headers: dict[str, str], **extra: str) -> Any:
    return api.post(
        path, json=body, headers={**headers, "X-Request-Id": f"t-{next(_ids)}", **extra}
    )


# -- identity -------------------------------------------------------------------


def test_health_needs_no_key_and_says_little(api: TestClient) -> None:
    r = api.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and set(r.json()) == {"status", "version", "runs_executing"}


def test_every_other_request_is_authenticated_and_names_its_tenant(api: TestClient) -> None:
    assert api.get("/capabilities").status_code == 401
    r = api.get("/capabilities", headers={"Authorization": "Bearer " + "x" * 40})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"
    r = api.get("/capabilities", headers={"Authorization": f"Bearer {AGENT_KEY}"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "tenant_missing"
    r = api.get("/capabilities", headers={**READER, "X-Cua-Tenant": "desk"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "tenant_not_authorized"
    r = api.get("/capabilities", headers={**AGENT, "X-Cua-Tenant": "nowhere"})
    assert r.status_code == 403


def test_a_post_names_itself_and_every_answer_carries_the_request_id(api: TestClient) -> None:
    r = api.post("/runs", json=BALANCE, headers=AGENT)
    assert r.status_code == 400 and r.json()["error"]["code"] == "request_id_invalid"
    r = api.post("/runs", json=BALANCE, headers={**AGENT, "X-Request-Id": "bad id!"})
    assert r.status_code == 400
    r = post(api, "/runs", BALANCE, AGENT, **{"X-Request-Id": "agent-req-1"})
    assert r.headers["X-Request-Id"] == "agent-req-1"
    assert r.json()["request_id"] == "agent-req-1"
    r = api.get("/capabilities", headers=AGENT)
    assert r.headers["X-Request-Id"].startswith("req_")


def test_a_client_with_no_usable_key_is_disabled_not_let_in() -> None:
    gate = Gate(access(), environ={"AGENT_KEY": AGENT_KEY, "READER_KEY": "short"})
    assert "reader" in gate.disabled and "shorter than" in gate.disabled["reader"]
    gate = Gate(access(), environ={"AGENT_KEY": AGENT_KEY})
    assert gate.disabled["reader"] == "no key at $READER_KEY"


def test_an_access_file_must_declare_every_tenant_a_client_names() -> None:
    data = access().model_dump(mode="json")
    data["clients"]["reader"]["tenants"] = ["elsewhere"]
    with pytest.raises(ValueError, match="undeclared tenant"):
        AccessConfig.model_validate(data)


def test_file_bound_keys_are_created_once_and_never_replaced(tmp_path: Path) -> None:
    config = AccessConfig.model_validate(
        {
            "tenants": {"local": {}},
            "clients": {
                "a": {"key": {"provider": "file", "path": "keys/a.key"}, "tenants": ["local"]}
            },
        }
    )
    [path] = create_keys(config, root=tmp_path)
    first = path.read_text()
    assert len(first.strip()) >= 32
    assert create_keys(config, root=tmp_path) == [] and path.read_text() == first


def test_the_committed_access_file_loads() -> None:
    config = load_access(REPO / "api" / "access.yaml")
    assert set(config.tenants) == {"local", "desk"}
    assert "approve" in config.clients["local-agent"].scopes
    assert config.clients["balance-reader"].capabilities == ["member_savings_balance"]


# -- capabilities ---------------------------------------------------------------


def test_capabilities_are_those_of_the_tenants_app_family_the_client_may_use(
    api: TestClient,
) -> None:
    names = [c["name"] for c in api.get("/capabilities", headers=AGENT).json()["capabilities"]]
    assert names == ["member_savings_balance", "open_subaccount"]
    names = [c["name"] for c in api.get("/capabilities", headers=READER).json()["capabilities"]]
    assert names == ["member_savings_balance"]
    desk = api.get("/capabilities", headers={**AGENT, "X-Cua-Tenant": "desk"}).json()
    assert {c["name"] for c in desk["capabilities"]} == {"deskcalc_compute", "deskcalc_record"}


def test_a_capability_is_described_never_handed_over(api: TestClient) -> None:
    r = api.get("/capabilities/open_subaccount", headers=AGENT)
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 3 and body["status"] == "approved" and body["invocable"]
    assert body["needs_idempotency_key"] and body["may_escalate"]
    assert body["tool"]["input_schema"]["required"] == ["member_id", "initial_deposit"]
    text = r.text
    assert "secret://" not in text and "steps" not in body and "path" not in body
    versions = api.get("/capabilities/open_subaccount/versions", headers=AGENT).json()
    assert versions["default"] == 3
    assert [v["version"] for v in versions["versions"] if v["default"]] == [3]


def test_capability_authorization(api: TestClient) -> None:
    r = api.get("/capabilities/open_subaccount", headers=READER)
    assert r.status_code == 403 and r.json()["error"]["code"] == "capability_not_authorized"
    assert api.get("/capabilities/nope", headers=AGENT).status_code == 404
    # Another tenant's application family is not this tenant's capability.
    r = api.get("/capabilities/deskcalc_compute", headers=AGENT)
    assert r.status_code == 404


# -- starting runs ----------------------------------------------------------------


def test_a_run_is_invoked_as_replay_would_be_and_answers_with_its_result(
    api: TestClient, fake: FakeRunner
) -> None:
    r = post(api, "/runs?wait=10", BALANCE, AGENT)
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["state"] == "finished" and run["client"] == "agent" and run["tenant"] == "local"
    assert run["result"]["kind"] == "success"
    assert run["result"]["outputs"] == {"savings_balance": "1411.21"}
    assert list(run["result"])[:3] == ["kind", "step_id", "side_effect"]  # as the CLI prints
    [call] = fake.calls
    assert call["run_id"] == run["run_id"]
    assert call["path"].parent.name == "member_savings_balance"  # the registered copy
    assert call["tenant"].id == "local" and call["handoff"] is None
    assert call["invocation"].inputs == {"member_id": "10003"}
    again = api.get(f"/runs/{run['run_id']}", headers=AGENT)
    assert again.status_code == 200 and again.json()["result"] == run["result"]


def test_without_wait_a_run_is_accepted_and_read_later(api: TestClient, fake: FakeRunner) -> None:
    fake.gate = threading.Event()
    r = post(api, "/runs", BALANCE, AGENT)
    assert r.status_code == 202 and r.json()["state"] == "running"
    assert r.headers["Location"] == f"/runs/{r.json()['run_id']}"
    fake.gate.set()
    done = api.get(f"{r.headers['Location']}?wait=10", headers=AGENT)
    assert done.status_code == 200 and done.json()["state"] == "finished"


def test_numbers_reach_the_capability_as_they_were_sent(api: TestClient, fake: FakeRunner) -> None:
    body = (
        '{"capability": "open_subaccount", '
        '"inputs": {"member_id": "10003", "initial_deposit": 250.00}}'
    )
    r = api.post(
        "/runs?wait=10",
        content=body,
        headers={**AGENT, "X-Request-Id": "dec-1", "Idempotency-Key": "dec"},
    )
    assert r.status_code == 200, r.text
    assert fake.calls[0]["invocation"].inputs["initial_deposit"] == "250.00"


def test_a_malformed_argument_is_input_invalid_with_nothing_started(
    api: TestClient, fake: FakeRunner
) -> None:
    r = post(api, "/runs", {**BALANCE, "inputs": {"member_id": 10003}}, AGENT)
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["kind"] == "failure" and result["code"] == "INPUT_INVALID"
    assert "must be a JSON string" in result["message"] and fake.calls == []


def test_a_request_body_that_is_not_a_run_request_is_refused(api: TestClient) -> None:
    r = post(api, "/runs", {**BALANCE, "extra": 1}, AGENT)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    r = api.post("/runs", content="not json", headers={**AGENT, "X-Request-Id": "x1"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_json"
    r = post(api, "/runs", {"capability": "nope"}, AGENT)
    assert r.status_code == 404


def test_what_commits_needs_an_idempotency_key(api: TestClient, fake: FakeRunner) -> None:
    r = post(api, "/runs", OPEN, AGENT)
    assert r.status_code == 428 and r.json()["error"]["code"] == "idempotency_key_required"
    assert fake.calls == []


def test_one_key_starts_one_run_and_a_retry_is_answered_with_it(
    api: TestClient, fake: FakeRunner
) -> None:
    first = post(api, "/runs?wait=10", OPEN, AGENT, **{"Idempotency-Key": "k1"})
    retry = post(api, "/runs?wait=10", OPEN, AGENT, **{"Idempotency-Key": "k1"})
    assert retry.json()["run_id"] == first.json()["run_id"]
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert len(fake.calls) == 1
    # The runner sees the key scoped to client and tenant; the caller sees its own.
    assert fake.calls[0]["invocation"].idempotency_key == "api:local:agent:k1"
    assert first.json()["result"]["idempotency_key"] == "k1"
    other = post(
        api,
        "/runs",
        {**OPEN, "inputs": {**OPEN["inputs"], "initial_deposit": 999}},
        AGENT,
        **{"Idempotency-Key": "k1"},
    )
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_conflict"


def test_concurrent_requests_with_one_key_start_one_run(api: TestClient, fake: FakeRunner) -> None:
    fake.gate = threading.Event()
    answers: list[str] = []

    def call() -> None:
        r = post(api, "/runs", BALANCE, AGENT, **{"Idempotency-Key": "race"})
        answers.append(r.json()["run_id"])

    threads = [threading.Thread(target=call) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    fake.gate.set()
    assert len(set(answers)) == 1 and len(fake.calls) == 1


def test_keys_are_scoped_to_the_client(tmp_path: Path, fake: FakeRunner) -> None:
    with make_app(tmp_path / "runs", fake) as api:
        a = post(api, "/runs?wait=10", BALANCE, AGENT, **{"Idempotency-Key": "same"})
        b = post(api, "/runs?wait=10", BALANCE, READER, **{"Idempotency-Key": "same"})
    assert a.json()["run_id"] != b.json()["run_id"] and len(fake.calls) == 2
    assert fake.calls[1]["invocation"].idempotency_key == "api:local:reader:same"


def test_scopes_and_capability_authorization_on_runs(api: TestClient, fake: FakeRunner) -> None:
    r = post(api, "/runs", OPEN, READER, **{"Idempotency-Key": "r1"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "capability_not_authorized"
    r = post(api, "/runs", {**BALANCE, "approval": {"token": "cat1.x.y"}}, READER)
    assert r.status_code == 403 and r.json()["error"]["code"] == "scope_missing"
    assert fake.calls == []


def test_a_run_is_visible_only_to_the_client_and_tenant_that_started_it(api: TestClient) -> None:
    run_id = post(api, "/runs?wait=10", BALANCE, AGENT).json()["run_id"]
    assert api.get(f"/runs/{run_id}", headers=READER).status_code == 404
    assert api.get(f"/runs/{run_id}", headers={**AGENT, "X-Cua-Tenant": "desk"}).status_code == 404
    assert api.get("/runs/..%2Fetc", headers=AGENT).status_code == 404


def test_inject_is_for_demos_only(tmp_path: Path, fake: FakeRunner, api: TestClient) -> None:
    r = post(api, "/runs", {**BALANCE, "inject": "slow_confirm"}, AGENT)
    assert r.status_code == 400 and r.json()["error"]["code"] == "inject_not_allowed"
    with make_app(tmp_path / "other", fake, allow_inject=True) as demo:
        r = post(demo, "/runs?wait=10", {**BALANCE, "inject": "slow_confirm"}, AGENT)
    assert r.status_code == 200 and fake.calls[-1]["invocation"].inject == "slow_confirm"


def test_a_request_that_cannot_be_made_is_an_error_with_no_result(
    api: TestClient, fake: FakeRunner
) -> None:
    fake.answer = "broken"
    run = post(api, "/runs?wait=10", BALANCE, AGENT).json()
    assert run["state"] == "error" and run["result"] is None
    assert "is not set" in run["error"]


# -- an escalated run -------------------------------------------------------------


def escalate(api: TestClient, fake: FakeRunner, key: str = "k1") -> str:
    fake.answer = "escalated"
    body = {**OPEN, "handoff": {"ttl_s": 600}}
    run = post(api, "/runs?wait=10", body, AGENT, **{"Idempotency-Key": key}).json()
    assert run["state"] == "escalated" and run["result"]["reason"] == "NEEDS_APPROVAL"
    handoff = fake.calls[-1]["handoff"]
    assert handoff.wait_s == 0 and handoff.ttl_s == 600
    return str(run["run_id"])


def test_approve_with_a_refused_token_leaves_the_run_waiting(
    api: TestClient, fake: FakeRunner
) -> None:
    run_id = escalate(api, fake)
    r = post(api, f"/runs/{run_id}/approve", {"token": "bad"}, AGENT)
    assert r.status_code == 403 and r.json()["error"]["code"] == "approval_refused"
    run = api.get(f"/runs/{run_id}", headers=AGENT).json()
    assert run["state"] == "escalated" and "approval refused" in run["error"]
    assert fake.resumes == []


def test_approve_carries_the_run_on_with_the_consent(api: TestClient, fake: FakeRunner) -> None:
    run_id = escalate(api, fake)
    r = post(api, f"/runs/{run_id}/approve?wait=10", {"token": "good", "approved_by": "rev"}, AGENT)
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["state"] == "finished" and run["result"]["side_effect"] == "committed"
    assert run["result"]["idempotency_key"] == "k1"
    [call] = fake.resumes
    assert call["token"] == "rt_secret" and call["approval"].token == "good"
    assert call["by"] == "api:agent" and call["wait_s"] == 0
    assert len(run["requests"]) == 2
    again = post(api, f"/runs/{run_id}/approve", {"token": "good"}, AGENT)
    assert again.status_code == 409 and again.json()["error"]["code"] == "run_not_escalated"


def test_approve_is_for_a_run_that_asked_for_consent(api: TestClient, fake: FakeRunner) -> None:
    run_id = escalate(api, fake)
    path = next((api.app.state.service.root / "runs").glob(f"{run_id}.json"))  # type: ignore[attr-defined]
    record = ApiRun.model_validate_json(path.read_text())
    record.result = {**(record.result or {}), "reason": "STUCK"}
    path.write_text(record.model_dump_json())
    r = post(api, f"/runs/{run_id}/approve", {"token": "good"}, AGENT)
    assert r.status_code == 409 and r.json()["error"]["code"] == "approval_not_asked"


def test_approve_and_operate_need_their_scopes(api: TestClient, fake: FakeRunner) -> None:
    run_id = escalate(api, fake)
    tight = access(
        clients={
            "agent": {
                "key": {"provider": "env", "var": "AGENT_KEY"},
                "tenants": ["local"],
                "capabilities": ["*"],
                "scopes": ["read", "invoke"],
            }
        }
    )
    api.app.state.gate = Gate(tight, environ={"AGENT_KEY": AGENT_KEY})  # type: ignore[attr-defined]
    for action, body in (("approve", {"token": "good"}), ("resume", {}), ("abort", {})):
        r = post(api, f"/runs/{run_id}/{action}", body, AGENT)
        assert r.status_code == 403 and r.json()["error"]["code"] == "scope_missing", action


def test_resume_hands_back_without_consent(api: TestClient, fake: FakeRunner) -> None:
    run_id = escalate(api, fake)
    r = post(api, f"/runs/{run_id}/resume?wait=10", {"resume_at": "review.submit"}, AGENT)
    assert r.status_code == 200
    [call] = fake.resumes
    assert call["approval"] is None and call["resume_at"] == "review.submit"


def test_a_run_answered_elsewhere_is_read_from_its_run_directory(
    api: TestClient, fake: FakeRunner, tmp_path: Path
) -> None:
    run_id = escalate(api, fake)
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    aborted = Failure(
        code="ESCALATION_ABORTED",
        message="aborted by ops",
        capability="open_subaccount",
        capability_version=3,
        run_id=run_id,
        idempotency_key="api:local:agent:k1",
    )
    (run_dir / "result.json").write_text(aborted.model_dump_json())
    run = api.get(f"/runs/{run_id}", headers=AGENT).json()
    assert run["state"] == "finished" and run["result"]["code"] == "ESCALATION_ABORTED"
    assert run["result"]["idempotency_key"] == "k1"


def test_a_run_cut_off_by_a_stopped_server_is_lost_not_assumed(
    tmp_path: Path, fake: FakeRunner
) -> None:
    """A record left ``running`` by a server that stopped: with no result in
    its run directory it is ``lost``; with one (the engine writes its result
    even when it is interrupted), that result."""
    runs = tmp_path / "runs"
    for run_id in ("run_CUT", "run_WROTE"):
        record = ApiRun(
            run_id=run_id,
            state="running",
            capability="member_savings_balance",
            tenant="local",
            client="agent",
            request_id="r",
            created_at="2026-09-27T00:00:00.000Z",
            updated_at="2026-09-27T00:00:00.000Z",
        )
        (runs / ".api" / "runs").mkdir(parents=True, exist_ok=True)
        (runs / ".api" / "runs" / f"{run_id}.json").write_text(record.model_dump_json())
    (runs / "run_WROTE").mkdir()
    interrupted = Failure(
        code="INTERRUPTED",
        side_effect="unknown",
        capability="member_savings_balance",
        capability_version=3,
        run_id="run_WROTE",
    )
    (runs / "run_WROTE" / "result.json").write_text(interrupted.model_dump_json())
    with make_app(runs, fake) as later:
        cut = later.get("/runs/run_CUT", headers=AGENT).json()
        wrote = later.get("/runs/run_WROTE", headers=AGENT).json()
    assert cut["state"] == "lost" and cut["result"] is None and "read its evidence" in cut["error"]
    assert wrote["state"] == "finished" and wrote["result"]["side_effect"] == "unknown"


def test_events_of_a_run_refused_before_it_started_are_none(api: TestClient) -> None:
    run_id = post(api, "/runs", {**BALANCE, "inputs": {"member_id": 1}}, AGENT).json()["run_id"]
    r = api.get(f"/runs/{run_id}/events", headers=AGENT)
    assert r.status_code == 200 and r.json()["events"] == []


def test_every_request_is_on_record(api: TestClient, tmp_path: Path) -> None:
    post(api, "/runs?wait=10", BALANCE, AGENT, **{"X-Request-Id": "audited-1"})
    api.get("/capabilities", headers=READER)
    lines = [
        json.loads(line)
        for line in (tmp_path / "runs" / ".api" / "requests.jsonl").read_text().splitlines()
    ]
    first = next(line for line in lines if line["request_id"] == "audited-1")
    assert first["client"] == "agent" and first["tenant"] == "local" and first["status"] == 200
    assert any(line["client"] == "reader" and line["path"] == "/capabilities" for line in lines)
    text = (tmp_path / "runs" / ".api" / "requests.jsonl").read_text()
    assert AGENT_KEY not in text and READER_KEY not in text


def test_the_api_imports_no_model_client() -> None:
    code = (
        "import sys\n"
        "import cua.api.app, cua.api.service, cua.api.cli\n"
        "import cua.api.routes.runs, cua.api.routes.capabilities, cua.api.routes.approvals\n"
        "bad = [m for m in sys.modules if m == 'anthropic' or m.startswith(('anthropic.',"
        " 'google.genai', 'cua.agent'))]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=REPO
    )
    assert out.stdout.strip() == ""
