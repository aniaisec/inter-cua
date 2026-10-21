"""Saturation, queue lifecycle and bounded MCP transport, without UI."""

from __future__ import annotations

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from cua.api.access import Gate
from cua.api.models import ApiError, ApiRun, ResumeRequest, RunRequest
from cua.api.service import RunService, ServiceSettings, _fingerprint
from cua.evidence.logger import utc_now
from cua.mcp import tools
from cua.mcp.server import McpServer
from cua.persistence import PersistenceError, atomic_write_json
from cua.replay.invocation import Budget
from tests.unit.test_api import (
    AGENT,
    AGENT_KEY,
    BALANCE,
    READER_KEY,
    REPO,
    FakeRunner,
    access,
    make_app,
    post,
)


@contextmanager
def service_at(root, **settings):
    fake = FakeRunner()
    entered, release = threading.Event(), threading.Event()
    worker_ids = set()

    def replay(path, **kwargs):
        worker_ids.add(threading.get_ident())
        entered.set()
        assert release.wait(30), "test did not release its worker"
        return fake.replay(path, **kwargs)

    gate = Gate(access(), environ={"AGENT_KEY": AGENT_KEY, "READER_KEY": READER_KEY})
    caller = gate.caller(f"Bearer {AGENT_KEY}", "local", "admission-test")
    service = RunService(
        ServiceSettings(runs_dir=root, capabilities_dir=REPO / "capabilities", **settings),
        replay=replay,
        resume=fake.resume,
        check_resume=fake.check_resume,
    )
    try:
        yield service, caller, fake, entered, release, worker_ids
    finally:
        release.set()
        service.close()


def submit(service, caller, key):
    return service.submit(caller, RunRequest(**BALANCE, budget=Budget(timeout_s=7)), key)


def test_capacity_has_no_phantom_claims_and_retries_still_resolve(tmp_path, monkeypatch):
    with service_at(tmp_path, workers=1, max_pending_runs=1) as (
        service,
        caller,
        fake,
        entered,
        release,
        ids,
    ):
        first, _ = submit(service, caller, "first")
        assert entered.wait(10)
        second, _ = submit(service, caller, "second")
        queued = service.get(caller, second.run_id)
        assert queued.state == "queued" and queued.started_at is None
        assert queued.queued_at and queued.queue_expires_at and service.busy() == 1
        with pytest.raises(ApiError) as busy:
            submit(service, caller, "overflow")
        assert (busy.value.status, busy.value.code, busy.value.retry_after_s) == (
            503,
            "server_busy",
            1,
        )
        assert len(list((service.root / "idempotency").glob("*.json"))) == 2
        assert len(list((service.root / "runs").glob("*.json"))) == 2
        # A retry must use its accepted record, even if catalog lookup is now unavailable.
        with monkeypatch.context() as patch:
            patch.setattr(
                "cua.api.service.catalog.find", lambda *a, **k: pytest.fail("retry repinned")
            )
            retry, replayed = submit(service, caller, "second")
            assert replayed and retry.run_id == second.run_id
        with pytest.raises(ApiError, match="different request"):
            service.submit(
                caller,
                RunRequest(capability=BALANCE["capability"], inputs={"member_id": "10004"}),
                "second",
            )
        release.set()
        service.wait(first.run_id, 10)
        service.wait(second.run_id, 10)
        settled = service.get(caller, second.run_id)
        assert settled.state == "finished"
        assert settled.started_at >= settled.queued_at and settled.queue_duration_ms >= 0
        assert len(fake.calls) == 2 and len(ids) == 1
        assert all(call["invocation"].budget.timeout_s == 7 for call in fake.calls)


def test_concurrent_overflow_is_bounded_before_persistence(tmp_path):
    with service_at(tmp_path, workers=1, max_pending_runs=2) as (
        service,
        caller,
        fake,
        entered,
        release,
        ids,
    ):

        def attempt(index):
            try:
                return submit(service, caller, f"key-{index}")[0]
            except ApiError as exc:
                assert exc.status == 503
                return None

        with ThreadPoolExecutor(12) as pool:
            results = list(pool.map(attempt, range(12)))
        accepted = [record for record in results if record is not None]
        assert len(accepted) == 3 and entered.wait(10)
        assert len(service._active) == 3 and service.busy() == 1
        assert len(list((service.root / "idempotency").glob("*.json"))) == 3
        release.set()
        for record in accepted:
            service.wait(record.run_id, 10)
        assert len(fake.calls) == 3 and len(ids) == 1


@pytest.mark.parametrize("poll", [True, False])
def test_queue_expiry_never_dispatches_or_grows_a_cancelled_backlog(tmp_path, monkeypatch, poll):
    now = ["2026-10-20T20:00:00.000Z"]
    monkeypatch.setattr("cua.api.service.utc_now", lambda: now[0])
    with service_at(tmp_path, workers=1, max_pending_runs=1, queue_timeout_s=60) as (
        service,
        caller,
        fake,
        entered,
        release,
        _,
    ):
        first, _ = submit(service, caller, "first")
        assert entered.wait(10)
        second, _ = submit(service, caller, "second")
        now[0] = "2026-10-20T20:01:01.000Z"
        if poll:
            expired = service.get(caller, second.run_id)
            assert expired.result["code"] == "TIMEOUT"
            assert expired.result["side_effect"] == "none" and expired.started_at is None
            assert expired.queue_duration_ms == 61000
            # Keep the reservation until the executor drains it, so cancellation
            # cannot create an unbounded backlog behind a blocked worker.
            for _ in range(12):
                with pytest.raises(ApiError):
                    submit(service, caller, "overflow")
        release.set()
        service.wait(first.run_id, 10)
        service.wait(second.run_id, 10)
        assert len(fake.calls) == 1
        retry, replayed = submit(service, caller, "second")
        assert replayed and retry.result["code"] == "TIMEOUT" and retry.started_at is None


def test_shutdown_cancels_waiting_work_and_finishes_the_active_run(tmp_path):
    with service_at(tmp_path, workers=1, max_pending_runs=1) as (
        service,
        caller,
        fake,
        entered,
        release,
        _,
    ):
        first, _ = submit(service, caller, "first")
        assert entered.wait(10)
        second, _ = submit(service, caller, "second")
        with ThreadPoolExecutor(1) as pool:
            closing = pool.submit(service.close)
            for _ in range(100):
                if not service._accepting:
                    break
                threading.Event().wait(0.01)
            try:
                with pytest.raises(ApiError) as stopped:
                    submit(service, caller, "overflow")
                assert stopped.value.code == "service_stopping"
                waiting = service.get(caller, second.run_id)
                assert waiting.state == "finished" and waiting.result["side_effect"] == "none"
                assert waiting.result["code"] == "INTERRUPTED" and waiting.started_at is None
                retry, replayed = submit(service, caller, "second")
                assert replayed and retry.run_id == second.run_id and not closing.done()
            finally:
                release.set()
            closing.result(timeout=10)
        assert len(fake.calls) == 1 and service.get(caller, first.run_id).state == "finished"


def test_save_and_claim_faults_never_create_a_phantom_or_dispatch(tmp_path, monkeypatch):
    with service_at(tmp_path, workers=1) as (service, caller, fake, _, _, _):

        def fail_save(record):
            raise OSError("disk fault")

        with monkeypatch.context() as patch:
            patch.setattr(service, "_save", fail_save)
            with pytest.raises(OSError, match="disk fault"):
                submit(service, caller, "save-fault")
        assert not list((service.root / "idempotency").glob("*.json"))
        claim = service._claim

        def published_then_failed(*args, **kwargs):
            result = claim(*args, **kwargs)
            if kwargs.get("create", True):
                raise OSError("failed after publication")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(service, "_claim", published_then_failed)
            with pytest.raises(OSError, match="after publication"):
                submit(service, caller, "claim-fault")
        retry, replayed = submit(service, caller, "claim-fault")
        assert replayed and retry.result["code"] == "INTERRUPTED"
        assert retry.result["side_effect"] == "none" and not fake.calls


def test_http_overload_has_retry_after_and_no_phantom_claim(tmp_path):
    fake = FakeRunner()
    fake.gate = threading.Event()
    with make_app(tmp_path, fake, workers=1, max_pending_runs=0) as api:
        try:
            first = post(api, "/runs", BALANCE, AGENT, **{"Idempotency-Key": "first"})
            assert first.status_code == 202
            overflow = post(api, "/runs", BALANCE, AGENT, **{"Idempotency-Key": "overflow"})
            assert overflow.status_code == 503 and overflow.headers["Retry-After"] == "1"
            assert overflow.json()["error"]["code"] == "server_busy"
            retry = post(api, "/runs", BALANCE, AGENT, **{"Idempotency-Key": "first"})
            assert retry.json()["run_id"] == first.json()["run_id"]
            assert len(list((tmp_path / ".api" / "idempotency").glob("*.json"))) == 1
        finally:
            fake.gate.set()


def test_restarted_queue_returns_an_interruption_without_dispatch(tmp_path):
    with service_at(tmp_path) as (service, caller, _, _, _, _):
        record = ApiRun(
            run_id="undispatched",
            state="running",
            capability=BALANCE["capability"],
            version=3,
            tenant=caller.tenant.id,
            client=caller.client_id,
            request_id=caller.request_id,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        service._queue(record)
        service._save(record)
        body = RunRequest(**BALANCE, budget=Budget(timeout_s=7))
        service._claim(caller, "same", _fingerprint(body), record.run_id)
    with service_at(tmp_path) as (service, caller, fake, _, _, _):
        retry, replayed = submit(service, caller, "same")
        assert replayed and retry.run_id == "undispatched"
        assert retry.result["code"] == "INTERRUPTED" and retry.result["side_effect"] == "none"
        assert not fake.calls


def test_cancelled_queued_resume_keeps_its_existing_escalation(tmp_path):
    with service_at(tmp_path, workers=1, max_pending_runs=1) as (
        service,
        caller,
        fake,
        entered,
        release,
        _,
    ):
        release.set()
        fake.answer = "escalated"
        first, _ = submit(service, caller, "paused")
        service.wait(first.run_id, 10)
        original = service.get(caller, first.run_id)
        assert original.state == "escalated"
        entered.clear()
        release.clear()
        busy, _ = submit(service, caller, "busy")
        assert entered.wait(10)
        queued = service.resume(caller, original.run_id, ResumeRequest())
        assert queued.state == "queued" and queued.result == original.result
        with ThreadPoolExecutor(1) as pool:
            closing = pool.submit(service.close)
            try:
                for _ in range(100):
                    if not service._accepting:
                        break
                    threading.Event().wait(0.01)
                waiting = service.get(caller, original.run_id)
                assert waiting.state == "escalated" and waiting.result == original.result
                assert waiting.started_at is None and not fake.resumes
            finally:
                release.set()
            closing.result(timeout=10)
        assert service.get(caller, busy.run_id).state == "escalated" and not fake.resumes


@pytest.mark.parametrize(
    "change",
    [
        {"queued_at": None},
        {"queue_expires_at": "bad"},
        {"started_at": "2026-10-20T20:00:00Z"},
        {"version": None},
        {"queue_expires_at": "2026-10-20T20:00:00"},
    ],
)
def test_corrupt_waiting_records_never_dispatch(tmp_path, change):
    with service_at(tmp_path, workers=1, max_pending_runs=1) as (
        service,
        caller,
        fake,
        entered,
        release,
        _,
    ):
        submit(service, caller, "active")
        assert entered.wait(10)
        queued, _ = submit(service, caller, "queued")
        path = service._path(queued.run_id)
        valid = json.loads(path.read_text())
        atomic_write_json(path, {**valid, **change})
        try:
            with pytest.raises(PersistenceError):
                service.get(caller, queued.run_id)
            # The worker independently refuses corrupt queue authority too.
            release.set()
            service.wait(queued.run_id, 10)
            assert len(fake.calls) == 1
        finally:
            # Preserve a valid original only to let fixture shutdown complete.
            atomic_write_json(path, valid)


def test_mcp_dispatch_is_bounded_while_status_and_ping_stay_responsive(tmp_path):
    with service_at(tmp_path, workers=1, max_pending_runs=0, max_mcp_calls=1) as (
        service,
        caller,
        fake,
        entered,
        release,
        _,
    ):
        server = McpServer(service, lambda rid: caller, wait_s=10)

        def request(rid, method="tools/call", **params):
            return (
                json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n"
            )

        def messages():
            yield request("first", name=BALANCE["capability"], arguments=BALANCE["inputs"])
            assert entered.wait(10)
            [run_id] = list(service._active)
            assert len([t for t in threading.enumerate() if t.name.startswith("cua-mcp-call")]) == 1
            for index in range(50):
                yield request(index, name=BALANCE["capability"], arguments=BALANCE["inputs"])
            yield request("status", name=tools.STATUS, arguments={"run_id": run_id, "wait_s": 300})
            yield request("ping", "ping")

        class Output(io.StringIO):
            def write(self, text):
                result = super().write(text)
                if json.loads(text)["id"] == "ping":
                    release.set()
                return result

        out = Output()
        server.serve(messages(), out)
        results = [json.loads(line) for line in out.getvalue().splitlines()]
        assert len(results) == 53 and len(fake.calls) == 1
        busy = [result for result in results if isinstance(result["id"], int)]
        assert len(busy) == 50
        assert all(
            item["result"]["structuredContent"]["error"]["retry_after_s"] == 1 for item in busy
        )
        assert all(
            item["result"]["structuredContent"]["error"]["code"] == "server_busy" for item in busy
        )
        assert [item["id"] for item in results][-3:] == ["status", "ping", "first"]
        assert results[-3]["result"]["structuredContent"]["state"] == "running"
        assert not [t for t in threading.enumerate() if t.name.startswith("cua-mcp-call")]


@pytest.mark.parametrize(
    "options",
    [
        {"workers": 0},
        {"workers": True},
        {"max_pending_runs": -1},
        {"max_mcp_calls": 0},
        {"queue_timeout_s": 0},
        {"queue_timeout_s": True},
        {"queue_timeout_s": 0.0001},
        {"queue_timeout_s": float("nan")},
        {"queue_timeout_s": float("inf")},
        {"queue_timeout_s": 86401},
    ],
)
def test_invalid_capacity_settings_touch_no_runtime_state(tmp_path, options):
    with pytest.raises(ValueError):
        ServiceSettings(runs_dir=tmp_path / "runs", **options)
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize(
    "command, flag, value",
    [
        ("serve", "--workers", "0"),
        ("serve", "--max-pending-runs", "-1"),
        ("serve", "--queue-timeout", "nan"),
        ("mcp", "--workers", "0"),
        ("mcp", "--max-mcp-calls", "0"),
        ("mcp", "--queue-timeout", "inf"),
    ],
)
def test_bad_cli_limits_fail_before_creating_keys(tmp_path, capsys, command, flag, value):
    from cua.cli import main

    assert main([command, flag, value, "--access", str(tmp_path / "absent.yaml")]) == 64
    error = capsys.readouterr().err
    assert "must be" in error and "absent.yaml" not in error
    assert not list(tmp_path.iterdir())


def test_original_service_settings_positional_arguments_keep_their_meaning(tmp_path):
    settings = ServiceSettings(
        tmp_path, REPO / "capabilities", "http://127.0.0.1:8100", 1, True, None
    )
    assert settings.workers == 1 and settings.allow_inject is True and settings.project is None
    assert settings.max_pending_runs == 32 and settings.max_mcp_calls == 8


@pytest.mark.parametrize("run_id", ["missing", "../../outside"])
def test_dangling_or_invalid_claim_is_corruption_and_never_new_work(tmp_path, run_id):
    with service_at(tmp_path) as (service, caller, fake, _, release, _):
        release.set()
        record, _ = submit(service, caller, "first")
        service.wait(record.run_id, 10)
        [path] = list((service.root / "idempotency").glob("*.json"))
        claim = json.loads(path.read_text())
        atomic_write_json(path, {**claim, "run_id": run_id})
        original = path.read_bytes()
        with pytest.raises(PersistenceError):
            submit(service, caller, "first")
        assert path.read_bytes() == original and len(fake.calls) == 1
