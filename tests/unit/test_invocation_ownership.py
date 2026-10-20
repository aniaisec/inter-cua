"""Durable reservations: contention and killed workers must never redispatch."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import pytest

from cua.persistence import PersistenceError, atomic_write_json
from cua.policy.tokens import SpentTokens
from cua.replay import runner
from cua.replay.invocation import Invocation
from cua.replay.ownership import (
    InvocationBusy,
    OwnedInvocation,
    SQLiteInvocationStore,
    execution_lock,
    identity,
)
from cua.replay.result import Escalated, Failure, IdempotencyCache, Success, fingerprint
from tests.unit.test_replay import CONSENT, GOAL1, GOAL2, POLICY, TENANT, engine, opened, review


def invoke(root, *, key="same", path=GOAL1, inputs=None):
    return runner.replay(
        path,
        tenant=TENANT,
        policy=POLICY,
        runs_dir=root,
        invocation=Invocation(inputs=inputs or {"member_id": "10003"}, idempotency_key=key),
    )


def answer(path, **kwargs):
    cap = runner.open_capability(path).capability
    return Success(
        capability=cap.name,
        capability_version=cap.version,
        run_id=kwargs["run_id"],
        idempotency_key=kwargs["invocation"].idempotency_key,
        outputs={"balance": "10"},
    )


def test_concurrent_requests_and_completed_retry_never_start_another_session(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def execute(path, **kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(10)
        return answer(path, **kwargs)

    monkeypatch.setattr(runner, "_replay", execute)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(invoke, tmp_path)
        try:
            assert entered.wait(10)
            with pytest.raises(runner.InvocationError, match="in progress"):
                invoke(tmp_path)
        finally:
            release.set()
        result = first.result(timeout=10)
    retry = invoke(tmp_path)
    assert retry.cached and retry.run_id == result.run_id
    assert len(calls) == 1
    conflict = invoke(tmp_path, inputs={"member_id": "10004"})
    assert conflict.code == "INPUT_INVALID"
    assert len(calls) == 1


@pytest.mark.parametrize("phase", ["reserved", "dispatched", "committed", "result"])
def test_killed_process_never_repeats_a_write(tmp_path, phase):
    # A test-only external commit oracle, independent of the invocation store.
    # Kill at four boundaries, including after acceptance but before persistence.
    code = """
import sys, time
from pathlib import Path
from cua.replay import runner
from cua.replay.ownership import SQLiteInvocationStore
from cua.replay.result import Success
from cua.persistence import atomic_write_json
from tests.unit.test_invocation_ownership import invoke, GOAL2
root, phase = Path(sys.argv[1]), sys.argv[2]
def stop():
    (root / "ready").write_text("ready")
    while True: time.sleep(.02)
save = SQLiteInvocationStore.save
def save_hook(self, name, record):
    save(self, name, record)
    if phase == "reserved" and record.phase == "reserved": stop()
SQLiteInvocationStore.save = save_hook
def execute(path, **kw):
    if phase == "dispatched": stop()
    with (root / "oracle").open("a") as f:
        f.write("commit\\n")
        f.flush()
        import os
        os.fsync(f.fileno())
    if phase == "committed": stop()
    cap = runner.open_capability(path).capability
    result = Success(capability=cap.name, capability_version=cap.version,
        run_id=kw["run_id"], outputs={}, side_effect="committed")
    atomic_write_json(root / kw["run_id"] / "result.json", result.model_dump(mode="json"))
    stop()
runner._replay = execute
invoke(root, path=GOAL2, inputs={"member_id":"10003", "initial_deposit":"10"})
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(tmp_path), phase])
    try:
        deadline = time.monotonic() + 20
        while not (tmp_path / "ready").exists():
            assert process.poll() is None, "child failed before its fault boundary"
            assert time.monotonic() < deadline
            time.sleep(0.02)
        # While alive, even a second CLI process cannot obtain ownership.
        busy = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; "
                "from tests.unit.test_invocation_ownership import invoke, GOAL2; "
                "invoke(Path(sys.argv[1]), path=GOAL2, "
                "inputs={'member_id':'10003','initial_deposit':'10'})",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert busy.returncode != 0 and "in progress" in busy.stderr
    finally:
        process.kill()
        process.wait(timeout=10)
    result = invoke(tmp_path, path=GOAL2, inputs={"member_id": "10003", "initial_deposit": "10"})
    if phase == "result":
        assert result.kind == "success" and result.side_effect == "committed"
    else:
        assert result.code == "INTERRUPTED"
        assert result.side_effect == ("none" if phase == "reserved" else "unknown")
    again = invoke(tmp_path, path=GOAL2, inputs={"member_id": "10003", "initial_deposit": "10"})
    assert again == result
    oracle = tmp_path / "oracle"
    assert (oracle.read_text().splitlines() if oracle.exists() else []) == (
        ["commit"] if phase in ("committed", "result") else []
    )


def test_legacy_answer_import_preserves_files_and_approval_seals(tmp_path, monkeypatch):
    cap = runner.open_capability(GOAL1).capability
    original = GOAL1.read_bytes()
    legacy = Failure(
        code="INTERRUPTED",
        capability=cap.name,
        capability_version=cap.version,
        side_effect="unknown",
    )
    IdempotencyCache(tmp_path, TENANT.id).put(
        "same", fingerprint(cap.name, cap.version, {"member_id": "10003"}), legacy
    )
    before = {p: p.read_bytes() for p in (tmp_path / ".idempotency").glob("*.json")}
    monkeypatch.setattr(runner, "_replay", lambda *a, **k: pytest.fail("legacy retry dispatched"))
    result = invoke(tmp_path)
    assert result.cached and result.side_effect == "unknown"
    assert before == {p: p.read_bytes() for p in before}
    assert GOAL1.read_bytes() == original
    record = SQLiteInvocationStore(tmp_path).get(identity(TENANT.id, "same"))
    assert record.phase == "finished"


def test_legacy_unanswered_run_is_refused_and_corruption_is_not_a_miss(tmp_path):
    atomic_write_json(
        tmp_path / "old" / "run.json",
        {
            "kind": "replay",
            "tenant": {"id": TENANT.id},
            "request": {"idempotency_key": "same"},
        },
    )
    with pytest.raises(runner.InvocationError, match="legacy invocation"):
        invoke(tmp_path)
    path = tmp_path / "old" / "run.json"
    path.write_text('{"kind":')
    with pytest.raises(PersistenceError):
        invoke(tmp_path)


def test_deleted_or_corrupted_database_never_authorizes_a_new_run(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_replay", answer)
    invoke(tmp_path)
    path = tmp_path / ".invocations" / "invocations.sqlite3"
    data = path.read_bytes()
    path.unlink()
    with pytest.raises(PersistenceError):
        invoke(tmp_path)
    path.write_bytes(b"corrupt")
    with pytest.raises(PersistenceError):
        invoke(tmp_path)
    path.write_bytes(data)
    assert invoke(tmp_path).cached


def test_immutable_completed_answers_and_store_contains_hashes_not_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_replay", answer)
    invoke(tmp_path)
    store = SQLiteInvocationStore(tmp_path)
    name = identity(TENANT.id, "same")
    record = store.get(name)
    assert record.phase == "finished"
    with pytest.raises(ValueError, match="immutable"):
        store.save(name, record.model_copy(update={"side_effect": "unknown"}))
    assert "10003" not in record.model_dump_json()
    assert "same" not in record.model_dump_json()


def test_malformed_sqlite_record_is_not_a_cache_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_replay", answer)
    invoke(tmp_path)
    store = SQLiteInvocationStore(tmp_path)
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("UPDATE invocations SET record=?", ('{"tenant":"local","tenant":"other"}',))
    monkeypatch.setattr(runner, "_replay", lambda *a, **kw: pytest.fail("corruption dispatched"))
    with pytest.raises(PersistenceError):
        invoke(tmp_path)


def test_edit_between_reservation_and_dispatch_starts_no_session(tmp_path, monkeypatch):
    path = tmp_path / "cap.json"
    path.write_bytes(GOAL1.read_bytes())
    save = SQLiteInvocationStore.save

    def edit(self, name, record):
        save(self, name, record)
        if record.phase == "dispatched":
            data = json.loads(path.read_text())
            data["description"] += " edited"
            atomic_write_json(path, data)

    monkeypatch.setattr(SQLiteInvocationStore, "save", edit)
    monkeypatch.setattr(runner, "_credentials", lambda *a, **kw: pytest.fail("dispatch began"))
    with pytest.raises(runner.InvocationError, match="content changed before dispatch"):
        invoke(tmp_path / "runs", path=path)


def test_commit_reservation_happens_before_the_surface_action(tmp_path):
    run, surface = engine(tmp_path, [review()])
    calls = []
    act = surface.act

    def guarded_action(action):
        if action.action == "click":
            assert calls == ["reserved"]
        return act(action)

    # _permit reserves approval; _act also persists the committing phase.
    run.before_commit = lambda grant: calls.append("reserved") if not calls else None
    surface.act = guarded_action
    run.run()
    assert calls == ["reserved"]


def test_lock_contention_does_not_catch_timeouts_from_inside_the_owner(tmp_path):
    with pytest.raises(TimeoutError, match="work failed"):
        with execution_lock(tmp_path, "test"):
            raise TimeoutError("work failed")
    with execution_lock(tmp_path, "test"):
        with pytest.raises(InvocationBusy):
            with execution_lock(tmp_path, "test"):
                pytest.fail("recursive dispatch obtained ownership")
        pass


def test_same_token_competitors_can_dispatch_only_one_commit(tmp_path):
    spent = SpentTokens(tmp_path)
    barrier = threading.Barrier(2)
    clicks = []

    def compete(name):
        run, surface = engine(tmp_path / name, [review(), opened()])
        original = surface.act
        first = True

        def reserve(grant):
            nonlocal first
            if first:
                first = False
                barrier.wait(timeout=10)
            if grant is not None:
                spent.spend(grant, name)

        def dispatch(action):
            if action.action == "click":
                assert spent.spent_by(CONSENT) == name
                clicks.append(name)
            return original(action)

        run.before_commit = reserve
        surface.act = dispatch
        return run.run()

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(compete, name) for name in ("left", "right")]
        results = [future.result(timeout=20) for future in futures]
    assert len(clicks) == 1
    assert sorted(result.kind for result in results) == ["failure", "success"]
    blocked = next(result for result in results if result.kind == "failure")
    assert blocked.code == "POLICY_BLOCKED" and blocked.side_effect == "none"


@pytest.fixture
def paused(tmp_path, monkeypatch):
    cap = runner.open_capability(GOAL1).capability
    name = identity(TENANT.id, "same")
    record = OwnedInvocation(
        tenant=TENANT.id,
        key_hash=name,
        fingerprint=fingerprint(cap.name, cap.version, {"member_id": "10003"}),
        capability=cap.name,
        version=cap.version,
        content_sha256=cap.content_hash(),
        run_id="paused",
    )
    result = Escalated(
        capability=cap.name,
        capability_version=cap.version,
        run_id="paused",
        reason="STUCK",
        request_id="ticket",
        resume_token="resume",
        idempotency_key="same",
    )
    store = SQLiteInvocationStore(tmp_path)
    store.answer(name, record, result)
    atomic_write_json(tmp_path / "paused" / "result.json", result.model_dump(mode="json"))
    context = SimpleNamespace(
        tenant=TENANT, idempotency_key="same", content_sha256=cap.content_hash()
    )
    monkeypatch.setattr(
        runner,
        "Queue",
        lambda root: SimpleNamespace(
            by_token=lambda token: "ticket",
            run_dir=lambda entry: tmp_path / "paused",
        ),
    )
    monkeypatch.setattr(runner, "read_model", lambda *args: context)
    monkeypatch.setattr(
        runner,
        "_prepare_resume",
        lambda *args, **kwargs: runner._Ready(
            entry="ticket",
            run_dir=tmp_path / "paused",
            ctx=context,
            cap=cap,
            invocation=Invocation(inputs={"member_id": "10003"}),
            grant=None,
        ),
    )
    return store, name, cap


def test_escalated_retry_keeps_the_existing_run_and_resume_uses_its_lock(
    tmp_path, monkeypatch, paused
):
    store, name, cap = paused
    assert invoke(tmp_path).resume_token == "resume"
    entered, release = threading.Event(), threading.Event()

    def continue_run(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return Success(
            capability=cap.name, capability_version=cap.version, run_id="paused", outputs={}
        )

    monkeypatch.setattr(runner, "_resume", continue_run)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(runner.resume, "resume", runs_dir=tmp_path)
        try:
            assert entered.wait(10)
            with pytest.raises(runner.InvocationError, match="in progress"):
                runner.resume("resume", runs_dir=tmp_path)
            with pytest.raises(runner.InvocationError, match="in progress"):
                invoke(tmp_path)
        finally:
            release.set()
        assert first.result(timeout=10).kind == "success"
    assert invoke(tmp_path).kind == "success"
    assert store.get(name).phase == "finished"


def test_interruption_during_resume_does_not_reuse_a_stale_escalation(
    tmp_path, monkeypatch, paused
):
    store, name, _ = paused

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "_resume", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.resume("resume", runs_dir=tmp_path)
    result = runner.resume("resume", runs_dir=tmp_path)
    assert result.code == "INTERRUPTED" and result.side_effect == "unknown"
    assert invoke(tmp_path).code == "INTERRUPTED"
    assert store.get(name).phase == "finished"


def test_reconciliation_preserves_a_result_that_belongs_to_another_run(tmp_path, paused):
    store, name, _ = paused
    record = store.get(name)
    store.save(name, record.model_copy(update={"phase": "dispatched", "result": None}))
    path = tmp_path / "paused" / "result.json"
    result = json.loads(path.read_text())
    result["run_id"] = "another"
    atomic_write_json(path, result)
    original = path.read_bytes()
    with pytest.raises(PersistenceError):
        invoke(tmp_path)
    assert path.read_bytes() == original
    assert not (path.parent / "result.before_interruption.json").exists()
