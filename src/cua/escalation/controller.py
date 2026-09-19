"""Who has the controls, and the only ways that can change.

::

    AUTOMATION        -> PAUSED             the run hit something a person should see
    PAUSED            -> HUMAN_IN_CONTROL   an operator takes control
    PAUSED            -> ABORTED            nobody came in time
    HUMAN_IN_CONTROL  -> RESUMING           the operator hands back (and may say where)
    HUMAN_IN_CONTROL  -> ABORTED            the operator stops the run
    RESUMING          -> AUTOMATION         the resume-state search found a checkpoint
    RESUMING          -> PAUSED             nothing holds: a second request

Anything else raises ``IllegalTransition``. There is no edge from PAUSED
straight back to the automation: the automation does not get the session back
until a person has said so. An operator who approves, or asks for the step
again, without touching the browser still passes through HUMAN_IN_CONTROL — for
the moment of deciding, the controls were theirs.

The state lives in one file per run, ``control.json``, together with the
lease it implies; every change is appended to ``state_transitions.jsonl``.
Two processes write it — the run's own, and the operator console — so every
change is a read-check-write under a lock file. The file is the queue's single
source of truth: the console never talks to the run process directly, which is
why a run can be handed back after the process that started it has exited.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cua.escalation.lease import ControlLease, Holder
from cua.evidence.logger import utc_now

State = Literal["AUTOMATION", "PAUSED", "HUMAN_IN_CONTROL", "RESUMING", "ABORTED"]

TRANSITIONS: dict[State, frozenset[State]] = {
    "AUTOMATION": frozenset({"PAUSED"}),
    "PAUSED": frozenset({"HUMAN_IN_CONTROL", "ABORTED"}),
    "HUMAN_IN_CONTROL": frozenset({"RESUMING", "ABORTED"}),
    "RESUMING": frozenset({"AUTOMATION", "PAUSED"}),
    "ABORTED": frozenset(),
}

HOLDER: dict[State, Holder] = {
    "AUTOMATION": "automation",
    "RESUMING": "automation",  # the search looks, and may read the outputs
    "PAUSED": "none",
    "HUMAN_IN_CONTROL": "human",
    "ABORTED": "none",
}

CONTROL_FILE = "control.json"
TRANSITIONS_FILE = "state_transitions.jsonl"
LOCK_FILE = "control.lock"
LOCK_TIMEOUT_S = 10.0
LOCK_STALE_S = 30.0
"""A lock file older than this belongs to a process that died holding it."""


class IllegalTransition(Exception):
    """A change of control the state machine does not allow."""


class StaleRequest(Exception):
    """An answer to a request that is no longer the open one."""


def check(current: State, to: State) -> None:
    if to not in TRANSITIONS[current]:
        allowed = ", ".join(sorted(TRANSITIONS[current])) or "nothing (it is final)"
        raise IllegalTransition(f"{current} -> {to} is not allowed; from {current}: {allowed}")


class OperatorDecision(BaseModel):
    """What the person decided, recorded with the transition that carries it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["hand_back", "abort"]
    by: str
    resume_at: str | None = None
    retry: bool = False
    approved: bool = False
    why: str = ""


class ControlRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    state: State
    lease: ControlLease
    request_id: str | None = None
    """The open request, or the last one."""
    decision: OperatorDecision | None = None
    """The answer to ``request_id``, once there is one."""
    seq: int = 0
    ended: str | None = None
    """How the run ended (its result kind), once it has."""


class ControlStore:
    def __init__(self, run_dir: Path) -> None:
        self.dir = run_dir
        self.path = run_dir / CONTROL_FILE
        self.log_path = run_dir / TRANSITIONS_FILE
        self._lock_path = run_dir / LOCK_FILE

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def start(self, run_id: str) -> ControlRecord:
        """A run begins with the automation holding the controls."""
        now = utc_now()
        record = ControlRecord(
            run_id=run_id, state="AUTOMATION", lease=ControlLease(holder="automation", since=now)
        )
        with self._locked():
            self._write(record)
            self._append(None, record, by="automation", note="run started")
        return record

    def read(self) -> ControlRecord:
        for _ in range(50):
            try:
                return ControlRecord.model_validate_json(self.path.read_text(encoding="utf-8"))
            except (PermissionError, json.JSONDecodeError, ValueError):
                time.sleep(0.02)  # mid-replace on Windows; the next look sees it whole
        return ControlRecord.model_validate_json(self.path.read_text(encoding="utf-8"))

    def lease(self) -> ControlLease:
        return self.read().lease

    def transition(
        self,
        to: State,
        *,
        by: str,
        request_id: str | None = None,
        expect_request: str | None = None,
        decision: OperatorDecision | None = None,
        note: str = "",
    ) -> ControlRecord:
        """Move to ``to``, or raise. ``expect_request``: the request the caller
        is answering; an answer to an older one is ``StaleRequest``."""
        with self._locked():
            current = self.read()
            if expect_request is not None and current.request_id != expect_request:
                raise StaleRequest(
                    f"request {expect_request} is not the open request of {current.run_id}"
                )
            check(current.state, to)
            now = utc_now()
            holder = HOLDER[to]
            lease = (
                current.lease
                if holder == current.lease.holder
                else ControlLease(
                    holder=holder, since=now, request_id=request_id or current.request_id
                )
            )
            new_request = request_id or current.request_id
            record = current.model_copy(
                update={
                    "state": to,
                    "lease": lease,
                    "request_id": new_request,
                    "decision": decision
                    if decision is not None
                    else (None if new_request != current.request_id else current.decision),
                    "seq": current.seq + 1,
                }
            )
            self._write(record)
            self._append(current.state, record, by=by, note=note, decision=decision)
            return record

    def end(self, kind: str) -> None:
        """Note how the run ended; the state stays what it was."""
        with self._locked():
            current = self.read()
            self._write(current.model_copy(update={"ended": kind, "seq": current.seq + 1}))

    def transitions(self) -> list[dict[str, object]]:
        if not self.log_path.is_file():
            return []
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    # -- files ---------------------------------------------------------------

    def _write(self, record: ControlRecord) -> None:
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
        for _ in range(50):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:  # a reader has it open (Windows)
                time.sleep(0.02)
        os.replace(tmp, self.path)

    def _append(
        self,
        before: State | None,
        record: ControlRecord,
        *,
        by: str,
        note: str,
        decision: OperatorDecision | None = None,
    ) -> None:
        line = {
            "ts": utc_now(),
            "seq": record.seq,
            "from": before,
            "to": record.state,
            "holder": record.lease.holder,
            "by": by,
            "request_id": record.request_id,
        }
        if decision is not None:
            line["decision"] = decision.model_dump(exclude_defaults=True)
        if note:
            line["note"] = note
        with self.log_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(line) + "\n")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - self._lock_path.stat().st_mtime > LOCK_STALE_S:
                        self._lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{self._lock_path} is held by another process") from None
                time.sleep(0.02)
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            yield
        finally:
            self._lock_path.unlink(missing_ok=True)
