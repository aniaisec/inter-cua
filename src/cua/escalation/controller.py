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

from collections.abc import Iterator
from contextlib import contextmanager
from itertools import pairwise
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cua.escalation.lease import ControlLease, Holder
from cua.evidence.logger import utc_now
from cua.persistence import (
    append_jsonl,
    atomic_write_json,
    corrupt,
    file_lock,
    read_jsonl,
    read_model,
)

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


class _Transition(BaseModel):
    seq: int = Field(ge=0)
    ts: str
    before: State | None = Field(alias="from")
    to: State
    holder: Holder
    by: str
    request_id: str | None


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
            if self.path.exists():
                raise IllegalTransition(
                    "control state already exists; resume or reconcile this run"
                )
            if read_jsonl(self.log_path):
                raise corrupt(self.log_path)
            self._write(record)
            self._append(None, record, by="automation", note="run started")
        return record

    def read(self) -> ControlRecord:
        with self._locked():
            history = read_jsonl(self.log_path)
            record = read_model(self.path, ControlRecord)
            if not history:
                raise corrupt(self.log_path)
            try:
                transitions = [_Transition.model_validate(row) for row in history]
            except ValidationError:
                raise corrupt(self.log_path) from None
            # Legacy end() calls left gaps even in otherwise complete histories.
            # Require ordered, connected transitions rather than invent missing rows.
            if transitions[0].seq != 0 or any(
                after.seq <= before.seq or after.before != before.to
                for before, after in pairwise(transitions)
            ):
                raise corrupt(self.log_path)
            latest = transitions[-1]
            # Old end() records incremented the snapshot without appending a row.
            legacy_end = record.ended is not None and record.seq == latest.seq + 1
            if (
                (latest.seq != record.seq and not legacy_end)
                or latest.to != record.state
                or latest.holder != record.lease.holder
            ):
                raise corrupt(self.path)
            return record

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
            self._complete_legacy_end(current)
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
            self._complete_legacy_end(current)
            record = current.model_copy(update={"ended": kind, "seq": current.seq + 1})
            self._write(record)
            self._append(current.state, record, by="automation", note=f"run ended: {kind}")

    def transitions(self) -> list[dict[str, object]]:
        with self._locked():
            return read_jsonl(self.log_path)

    # -- files ---------------------------------------------------------------

    def _complete_legacy_end(self, record: ControlRecord) -> None:
        latest = read_jsonl(self.log_path)[-1]
        if record.ended is not None and record.seq == latest["seq"] + 1:
            self._append(record.state, record, by="migration", note="recorded legacy end marker")

    def _write(self, record: ControlRecord) -> None:
        atomic_write_json(self.path, record.model_dump(mode="json"))

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
        append_jsonl(self.log_path, line)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with file_lock(self._lock_path, timeout=LOCK_TIMEOUT_S):
            yield
