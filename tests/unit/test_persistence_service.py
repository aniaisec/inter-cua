"""Corrupt API/audit authority refuses dispatch across HTTP and MCP."""

from __future__ import annotations

import pytest

from cua.persistence import PersistenceError
from tests.unit.test_api import AGENT, BALANCE, FakeRunner, make_app, post
from tests.unit.test_mcp import call, make_server


@pytest.mark.parametrize("content", ["{", "[]", "{}", '{"fingerprint": "fp"}'])
def test_corrupt_http_claim_never_starts_another_run(tmp_path, content):
    fake = FakeRunner()
    with make_app(tmp_path, fake) as client:
        first = post(client, "/runs?wait=10", BALANCE, AGENT, **{"Idempotency-Key": "same"})
        assert first.status_code == 200
        [claim] = list((tmp_path / ".api" / "idempotency").glob("*.json"))
        claim.write_text(content)
        retry = post(client, "/runs?wait=10", BALANCE, AGENT, **{"Idempotency-Key": "same"})
        assert retry.status_code == 409
        assert retry.json()["error"]["code"] == "state_corrupt"
        assert len(fake.calls) == 1
        assert claim.read_text() == content


def test_corrupt_api_record_is_not_reported_as_missing(tmp_path):
    fake = FakeRunner()
    with make_app(tmp_path, fake) as client:
        first = post(client, "/runs?wait=10", BALANCE, AGENT)
        run_id = first.json()["run_id"]
        (tmp_path / ".api" / "runs" / f"{run_id}.json").write_text('{"state":')
        response = client.get(f"/runs/{run_id}", headers=AGENT)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "state_corrupt"
        assert len(fake.calls) == 1


def test_corrupt_result_is_not_turned_into_a_lost_run(tmp_path):
    fake = FakeRunner()
    with make_app(tmp_path, fake) as client:
        service = client.app.state.service
        run = tmp_path / "run_cut_off"
        run.mkdir()
        (run / "result.json").write_text('{"side_effect":')
        with pytest.raises(PersistenceError):
            service._stored(run.name)


def test_torn_audit_refuses_http_dispatch_and_preserves_tail(tmp_path):
    fake = FakeRunner()
    audit = tmp_path / ".api" / "requests.jsonl"
    audit.parent.mkdir()
    audit.write_text('{"request_id":"cut')
    with make_app(tmp_path, fake) as client:
        response = post(client, "/runs", BALANCE, AGENT)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "state_corrupt"
        assert fake.calls == []
        assert audit.read_text() == '{"request_id":"cut'


def test_torn_audit_is_a_structured_mcp_refusal_before_dispatch(tmp_path):
    fake = FakeRunner()
    server = make_server(tmp_path, fake)
    audit = server.service.root / "requests.jsonl"
    audit.parent.mkdir()
    audit.write_text('{"request_id":"cut')
    try:
        result = call(server, "member_savings_balance", member_id="10003")
        assert result["isError"]
        assert "state_corrupt" in str(result)
        assert fake.calls == []
        assert audit.read_text() == '{"request_id":"cut'
    finally:
        server.service.close()
