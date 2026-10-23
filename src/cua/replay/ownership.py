"""Local invocation authority. Evidence remains on disk; ownership lives in SQLite.

An OS-held lock spans execution, but SQLite transactions never span UI work.
Acquiring that lock proves a cooperating previous worker cannot still act. There
is deliberately no heartbeat timeout, lease stealing, or automatic redispatch.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cua.persistence import _object, atomic_write_json, corrupt, file_lock, read_json
from cua.replay.result import RESULT, Evidence, Failure, ReplayResult, SideEffect

VERSION = 1
_executing: set[str] = set()
_executing_lock = threading.Lock()


class InvocationBusy(Exception):
    """Another worker holds the execution lock. Retrying must preserve the key."""


class OwnedInvocation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    tenant: str
    key_hash: str
    fingerprint: str
    capability: str
    version: int = Field(ge=1)
    content_sha256: str
    run_id: str
    phase: Literal["reserved", "dispatched", "committing", "escalated", "finished"] = "reserved"
    side_effect: SideEffect = "none"
    result: dict[str, object] | None = None
    result_reference: str | None = None

    @model_validator(mode="after")
    def valid_answer(self) -> OwnedInvocation:
        if not self.run_id.replace("_", "").isalnum():
            raise ValueError("invalid run id")
        if self.phase in ("finished", "escalated") and self.result is None:
            raise ValueError("missing invocation answer")
        if self.result is not None:
            answer = RESULT.validate_python(self.result)
            if (
                answer.capability != self.capability
                or answer.capability_version != self.version
                or answer.run_id not in (None, self.run_id)
                or (self.phase == "escalated") != (answer.kind == "escalated")
                or self.phase not in ("finished", "escalated")
            ):
                raise ValueError("invocation answer mismatch")
        return self


class InvocationStore(Protocol):
    def get(self, identity: str) -> OwnedInvocation | None: ...

    def save(self, identity: str, record: OwnedInvocation) -> None: ...


def identity(tenant: str, key: str) -> str:
    return hashlib.sha256(json.dumps([tenant, key]).encode()).hexdigest()


@contextmanager
def execution_lock(root: Path, name: str) -> Iterator[None]:
    """Fail promptly on contention, including another thread in this process."""
    path = root / ".invocations" / "owners" / name
    local = os.path.normcase(str(path.resolve()))
    message = "invocation is in progress; retry with the same idempotency key"
    with _executing_lock:
        if local in _executing:
            raise InvocationBusy(message)
        _executing.add(local)
    try:
        lock = file_lock(path, timeout=0)
        try:
            lock.__enter__()
        except TimeoutError:
            raise InvocationBusy(message) from None
        try:
            yield
        finally:
            lock.__exit__(None, None, None)
    finally:
        with _executing_lock:
            _executing.remove(local)


class SQLiteInvocationStore:
    """Versioned, fail-closed authority on a cooperating single host.

    The marker makes accidental database deletion an error, rather than a new
    empty authority. Legacy answers are imported by the runner under its owner
    lock, with original JSON files preserved; seals and evidence are untouched.
    """

    @classmethod
    def existing(cls, root: Path) -> SQLiteInvocationStore:
        """Open existing authority without initialization or marker/lock writes."""
        store = object.__new__(cls)
        store.root = root
        store.dir = root / ".invocations"
        store.path = store.dir / "invocations.sqlite3"
        store._read_only = True
        if read_json(store.dir / "format.json") != {"version": VERSION}:
            raise corrupt(store.path)
        with store._connect():
            pass
        return store

    def __init__(self, root: Path) -> None:
        self._read_only = False
        self.root = root
        self.dir = root / ".invocations"
        self.path = self.dir / "invocations.sqlite3"
        marker = self.dir / "format.json"
        with file_lock(marker):
            if marker.exists():
                if read_json(marker) != {"version": VERSION} or not self.path.is_file():
                    raise corrupt(self.path)
            else:
                # A crash before marker publication can leave a fully initialized
                # DB; validate it, never reset or silently upgrade its schema.
                if not self.path.exists():
                    with closing(sqlite3.connect(self.path)) as db, db:
                        db.execute("PRAGMA synchronous=FULL")
                        db.execute(
                            "CREATE TABLE invocations "
                            "(identity TEXT PRIMARY KEY, record TEXT NOT NULL)"
                        )
                        db.execute(f"PRAGMA user_version={VERSION}")
                with self._connect():
                    pass
                atomic_write_json(marker, {"version": VERSION})

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # mode=rw must not recreate a missing authority, even after __init__.
        try:
            mode = "ro" if self._read_only else "rw"
            db = sqlite3.connect(
                self.path.resolve().as_uri() + f"?mode={mode}", uri=True, timeout=10
            )
            try:
                db.execute("PRAGMA synchronous=FULL")
                if db.execute("PRAGMA user_version").fetchone() != (VERSION,):
                    raise corrupt(self.path)
                if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise corrupt(self.path)
                db.execute("SELECT identity, record FROM invocations LIMIT 0")
                with db:
                    yield db
            finally:
                db.close()
        except sqlite3.DatabaseError:
            raise corrupt(self.path) from None

    def get(self, identity: str) -> OwnedInvocation | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT record FROM invocations WHERE identity=?", (identity,)
            ).fetchone()
        if row is None:
            return None
        try:
            record = OwnedInvocation.model_validate(_object(row[0]))
            if record.key_hash != identity:
                raise ValueError("invocation scope mismatch")
            return record
        except (ValueError, TypeError):
            raise corrupt(self.path) from None

    def records(self) -> list[OwnedInvocation]:
        """Validated snapshot for local maintenance; never changes ownership."""
        with self._connect() as db:
            rows = db.execute("SELECT identity, record FROM invocations").fetchall()
        try:
            records = [OwnedInvocation.model_validate(_object(row[1])) for row in rows]
            if any(
                len(record.key_hash) != 64
                or any(char not in "0123456789abcdef" for char in record.key_hash)
                for record in records
            ):
                raise ValueError("invalid ownership identity")
            if any(row[0] != record.key_hash for row, record in zip(rows, records, strict=True)):
                raise ValueError("invocation scope mismatch")
            return records
        except (ValueError, TypeError):
            raise corrupt(self.path) from None

    def save(self, identity: str, record: OwnedInvocation) -> None:
        # The owner lock serializes the complete read/check/update protocol.
        record = OwnedInvocation.model_validate(record.model_dump())
        if record.key_hash != identity:
            raise ValueError("invocation scope mismatch")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT record FROM invocations WHERE identity=?", (identity,)
            ).fetchone()
            if old is not None:
                try:
                    before = OwnedInvocation.model_validate(_object(old[0]))
                except (ValueError, TypeError):
                    raise corrupt(self.path) from None
                if before.phase == "finished" and before != record:
                    raise ValueError("a completed invocation is immutable")
                if any(
                    getattr(before, f) != getattr(record, f)
                    for f in (
                        "tenant",
                        "key_hash",
                        "fingerprint",
                        "capability",
                        "version",
                        "content_sha256",
                        "run_id",
                    )
                ):
                    raise ValueError("invocation identity is immutable")
            db.execute(
                "INSERT INTO invocations VALUES (?, ?) ON CONFLICT(identity) "
                "DO UPDATE SET record=excluded.record",
                (identity, record.model_dump_json()),
            )

    def answer(self, identity: str, record: OwnedInvocation, result: ReplayResult) -> None:
        self.save(
            identity,
            record.model_copy(
                update={
                    "phase": "escalated" if result.kind == "escalated" else "finished",
                    "side_effect": result.side_effect,
                    "result": result.model_copy(update={"idempotency_key": None}).model_dump(
                        mode="json"
                    ),
                    "result_reference": f"{record.run_id}/result.json"
                    if result.evidence.run_dir
                    else None,
                }
            ),
        )

    def reconcile(self, identity: str, record: OwnedInvocation, key: str | None) -> ReplayResult:
        """Called only under execution_lock: the previous owner cannot act."""
        path = self.root / record.run_id / "result.json"
        interrupted = False
        if path.exists():
            try:
                answer = RESULT.validate_python(read_json(path))
            except ValueError:
                raise corrupt(path) from None
            if (
                answer.capability != record.capability
                or answer.capability_version != record.version
                or answer.run_id != record.run_id
            ):
                raise corrupt(path)
            if answer.kind == "escalated" and record.phase not in ("escalated",):
                # A resume died while an older escalation was still on disk.
                # That checkpoint does not prove the resumed worker did nothing.
                previous = path.with_name("result.before_interruption.json")
                if not previous.exists():
                    atomic_write_json(previous, answer.model_dump(mode="json"))
                interrupted = True
                answer = Failure(
                    code="INTERRUPTED",
                    capability=record.capability,
                    capability_version=record.version,
                    run_id=record.run_id,
                    idempotency_key=key,
                    side_effect="unknown",
                    message="execution stopped with a stale escalation; reconcile before resuming",
                    evidence=Evidence(run_dir=path.parent.as_posix()),
                )
        elif record.phase == "escalated" and record.result is not None:
            answer = RESULT.validate_python(record.result)
        else:
            interrupted = True
            answer = Failure(
                code="INTERRUPTED",
                capability=record.capability,
                capability_version=record.version,
                run_id=record.run_id,
                idempotency_key=key,
                side_effect=record.side_effect,
                message="the previous invocation stopped without an answer; "
                "reconcile its evidence before any new attempt",
                evidence=Evidence(run_dir=(self.root / record.run_id).as_posix()),
            )
        if interrupted:
            # HTTP/workflow readers must see the interruption too, rather than
            # an older escalation. Preserve that escalation separately above.
            atomic_write_json(path, answer.model_dump(mode="json"))
        self.answer(identity, record, answer)
        return answer.model_copy(update={"cached": True, "idempotency_key": key})
