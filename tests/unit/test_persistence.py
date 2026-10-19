"""Faults, competing writers and corrupt authority must not silently reset state."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cua import persistence
from cua.escalation.controller import ControlStore
from cua.escalation.requests import Queue
from cua.evidence.logger import RunLog
from cua.persistence import (
    PersistenceConflict,
    PersistenceError,
    append_jsonl,
    atomic_write_json,
    file_lock,
    read_json,
    read_jsonl,
)
from cua.replay.result import Failure, IdempotencyCache
from cua.workflow.journal import Entry, Journal


def test_json_publication_uses_sibling_and_syncs_before_replacing(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"old": true}\n')
    calls = []
    replace = os.replace
    sync = persistence._sync

    def tracked_sync(fd):
        calls.append("sync")
        sync(fd)

    def tracked_replace(source, target):
        assert Path(source).parent == path.parent
        assert read_json(path) == {"old": True}
        assert calls == ["sync"]
        calls.append("replace")
        replace(source, target)

    monkeypatch.setattr(persistence, "_sync", tracked_sync)
    monkeypatch.setattr(os, "replace", tracked_replace)
    atomic_write_json(path, {"text": "café\nnext"})
    assert read_json(path) == {"text": "café\nnext"}
    assert b"\r\n" not in path.read_bytes()
    assert not list(tmp_path.glob("*.tmp"))
    assert calls[:2] == ["sync", "replace"]


@pytest.mark.parametrize("fault", ["sync", "replace"])
def test_failed_publication_preserves_previous_answer_and_cleans_temp(tmp_path, monkeypatch, fault):
    path = tmp_path / "state.json"
    original = b'{"side_effect": "unknown"}\n'
    path.write_bytes(original)

    def fail(*args):
        raise OSError("injected persistence fault")

    if fault == "sync":
        monkeypatch.setattr(persistence, "_sync", fail)
    else:
        monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="injected"):
        atomic_write_json(path, {"side_effect": "none"})
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_nonfinite_json_is_rejected_before_any_files_are_created(tmp_path):
    path = tmp_path / "new" / "state.json"
    with pytest.raises(ValueError):
        atomic_write_json(path, {"value": float("nan")})
    assert not path.parent.exists()


@pytest.mark.parametrize(
    "text",
    [
        "{",
        "[]",
        '{"a":1,"a":2}',
        '{"nested":{"a":1,"a":2}}',
        '{"a":NaN}',
        '{"a":1e999}',
        '{"secret":"do-not-echo",',
    ],
)
def test_malformed_state_is_refused_without_echoing_content(tmp_path, text):
    path = tmp_path / "state.json"
    path.write_text(text)
    with pytest.raises(PersistenceError) as error:
        read_json(path)
    assert "do-not-echo" not in str(error.value)
    assert "reconciliation" in str(error.value)


@pytest.mark.parametrize("tail", ['{"revoked":true}', '{"revoked":', "[]\n", '{"a":1,"a":2}\n'])
def test_torn_or_invalid_jsonl_cannot_be_ignored_or_extended(tmp_path, tail):
    path = tmp_path / "lifecycle.jsonl"
    original = ('{"approved":true}\n' + tail).encode()
    path.write_bytes(original)
    with pytest.raises(PersistenceError):
        read_jsonl(path)
    with pytest.raises(PersistenceError):
        append_jsonl(path, {"approved": True})
    assert path.read_bytes() == original


def test_threads_serialize_entire_updates_and_nested_locks(tmp_path):
    path = tmp_path / "counter.json"
    atomic_write_json(path, {"count": 0})

    def increment(_):
        for _ in range(8):
            with file_lock(path), file_lock(path):
                value = read_json(path)["count"]
                atomic_write_json(path, {"count": value + 1})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(increment, range(4)))
    assert read_json(path) == {"count": 32}


def test_run_log_instances_share_event_sequence_under_threads(tmp_path):
    logs = [RunLog(tmp_path) for _ in range(4)]

    def event(index):
        logs[index % 4].event("concurrent", index=index)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(event, range(24)))
    assert [row["seq"] for row in read_jsonl(tmp_path / "log.jsonl")] == list(range(1, 25))


def test_workflow_stale_snapshot_cannot_erase_another_attempt(tmp_path):
    first, second = Journal(tmp_path, "t"), Journal(tmp_path, "t")
    assert first.get("k") is second.get("k") is None
    entry = Entry(fingerprint="fp", workflow="wf", workflow_version=1, pins={}, attempts=["one"])
    first.put("k", entry)
    with pytest.raises(PersistenceConflict):
        second.put("k", entry.model_copy(update={"attempts": ["two"]}))
    assert Journal(tmp_path, "t").get("k").attempts == ["one"]


def test_cached_uncertain_answer_cannot_be_overwritten(tmp_path):
    cache = IdempotencyCache(tmp_path, "t")
    cache.put(
        "k",
        "fp",
        Failure(code="INTERRUPTED", side_effect="unknown", capability="c", capability_version=1),
    )
    cache.put(
        "k",
        "fp",
        Failure(code="INPUT_INVALID", side_effect="none", capability="c", capability_version=1),
    )
    assert cache.get("k", "fp").side_effect == "unknown"


@pytest.mark.parametrize("text", ["{}", "[]", '{"fingerprint":"fp","result":{}}'])
def test_corrupt_cache_is_not_a_miss_or_replaced_by_put(tmp_path, text):
    cache = IdempotencyCache(tmp_path, "t")
    path = cache._path("k")
    path.parent.mkdir()
    path.write_text(text)
    for action in (
        lambda: cache.get("k", "fp"),
        lambda: cache.put("k", "fp", Failure(code="TIMEOUT", capability="c", capability_version=1)),
    ):
        with pytest.raises(PersistenceError):
            action()
    assert path.read_text() == text


def test_control_refuses_snapshot_ahead_of_transition_log(tmp_path, monkeypatch):
    control = ControlStore(tmp_path)
    control.start("run_x")

    def fail(*args, **kwargs):
        raise OSError("interrupted append")

    monkeypatch.setattr(control, "_append", fail)
    with pytest.raises(OSError):
        control.transition("PAUSED", by="automation")
    with pytest.raises(PersistenceError):
        control.read()


def test_legacy_end_marker_can_be_resumed_without_a_sequence_gap(tmp_path):
    control = ControlStore(tmp_path)
    control.start("run_x")
    control.transition("PAUSED", by="automation", request_id="req_1")
    record = control.read()
    atomic_write_json(
        control.path,
        record.model_copy(update={"seq": record.seq + 1, "ended": "escalated"}).model_dump(
            mode="json"
        ),
    )
    control.transition("HUMAN_IN_CONTROL", by="operator", expect_request="req_1")
    assert control.read().state == "HUMAN_IN_CONTROL"
    rows = read_jsonl(control.log_path)
    assert [row["seq"] for row in rows] == [0, 1, 2, 3]
    assert rows[2]["by"] == "migration"


def test_corrupt_queue_entry_is_not_silently_hidden(tmp_path):
    queue = Queue(tmp_path)
    queue.dir.mkdir()
    (queue.dir / "req_bad.json").write_text('{"token_sha256":')
    with pytest.raises(PersistenceError):
        queue.entries()
    with pytest.raises(PersistenceError):
        queue.by_token("arbitrary-token")


def test_preexisting_legacy_history_gaps_remain_readable(tmp_path):
    control = ControlStore(tmp_path)
    control.start("run_x")
    control.transition("PAUSED", by="automation", request_id="req_1")
    record = control.read()
    atomic_write_json(
        control.path,
        record.model_copy(update={"seq": 2, "ended": "escalated"}).model_dump(mode="json"),
    )
    control.transition("HUMAN_IN_CONTROL", by="operator")
    # Before PR12 the end marker at seq=2 was never appended.
    rows = read_jsonl(control.log_path)
    control.log_path.write_text("".join(json.dumps(row) + "\n" for row in rows if row["seq"] != 2))
    assert control.read().state == "HUMAN_IN_CONTROL"
    control.transition("RESUMING", by="operator")
    assert control.read().state == "RESUMING"


def test_corrupt_cache_refuses_replay_before_surface_start(tmp_path):
    from tests.unit.test_registry import GOAL1, TENANT, run_replay

    cache = IdempotencyCache(tmp_path / "runs", TENANT.id)
    path = cache._path("k")
    path.parent.mkdir(parents=True)
    path.write_text('{"result":')
    # run_replay's surface factory raises if anything tries to open an application.
    with pytest.raises(PersistenceError):
        run_replay(GOAL1, tmp_path, idempotency_key="k")
    assert path.read_text() == '{"result":'


@pytest.mark.parametrize("error", [errno.EINVAL, errno.ENOSYS, errno.EIO])
def test_sync_ignores_only_unsupported_operations(monkeypatch, error):
    def fail(fd):
        raise OSError(error, "injected fsync error")

    monkeypatch.setattr(os, "fsync", fail)
    if error == errno.EIO:
        with pytest.raises(OSError):
            persistence._sync(1)
    else:
        persistence._sync(1)


def test_processes_serialize_updates_and_jsonl_appends(tmp_path):
    path = tmp_path / "count.json"
    log = tmp_path / "audit.jsonl"
    atomic_write_json(path, {"count": 0})
    code = """
import sys
from pathlib import Path
from cua.persistence import atomic_write_json, append_jsonl, read_json, file_lock
path, log = map(Path, sys.argv[1:])
for _ in range(8):
    with file_lock(path):
        value = read_json(path)['count'] + 1
        atomic_write_json(path, {'count': value})
        append_jsonl(log, {'count': value})
"""
    processes = [
        subprocess.Popen([sys.executable, "-c", code, str(path), str(log)]) for _ in range(3)
    ]
    try:
        assert [process.wait(timeout=20) for process in processes] == [0, 0, 0]
        assert read_json(path) == {"count": 24}
        assert [record["count"] for record in read_jsonl(log)] == list(range(1, 25))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_killed_writer_preserves_previous_json_and_releases_os_lock(tmp_path):
    path = tmp_path / "state.json"
    ready = tmp_path / "ready"
    atomic_write_json(path, {"old": True})
    code = """
import os, sys, time
from pathlib import Path
from cua.persistence import atomic_write_json, file_lock
path, ready = map(Path, sys.argv[1:])
def before_replace(*args):
    ready.write_text('ready')
    time.sleep(60)
os.replace = before_replace
with file_lock(path):
    atomic_write_json(path, {'new': True})
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(path), str(ready)])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "child did not reach the pre-replace crash point"
        # Age never permits a live lock to be stolen.
        sidecar = path.with_name(path.name + ".lock")
        os.utime(sidecar, (0, 0))
        with pytest.raises(TimeoutError):
            with file_lock(path, timeout=0.1):
                pytest.fail("stole a live writer's lock")
        process.kill()
        process.wait(timeout=10)
        assert read_json(path) == {"old": True}
        with file_lock(path, timeout=1):
            atomic_write_json(path, {"recovered": True})
        assert read_json(path) == {"recovered": True}
        # A killed writer may leave an inert staging file; it is never loaded as state.
        assert len(list(tmp_path.glob("*.tmp"))) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
