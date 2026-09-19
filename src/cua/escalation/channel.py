"""The handoff channel a run uses to ask for a person: files in, files out.

``open`` writes the intervention request into the run directory, posts it to
the queue, and pauses (automation → PAUSED, lease → none). ``wait`` then
watches ``control.json`` until the operator console — or ``cua resume`` —
answers, and turns the answer into a ``Decision``:

* RESUMING  -> ``HandBack`` (with the operator's ``resume_at``, retry, approval);
* ABORTED   -> ``Abort``;
* the request's time to live runs out -> ABORTED by ``timeout``, ``Abort``;
* nobody takes the request within ``wait_s``, or the waiting process is
  interrupted -> ``Unanswered``: the run returns ``escalated`` and the session
  stays up for ``cua resume``.

``wait_s`` bounds only the wait for someone to *pick up* the request. Once a
person holds the controls the process keeps waiting for them, up to the
request's time to live — walking away from a person mid-handoff would leave
them driving a session nobody is going to carry on.

While it waits, the channel keeps a heartbeat in ``waiter.json`` so the console
can tell the operator whether handing back resumes the run at once or leaves it
for its caller.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.escalation.controller import ControlStore, IllegalTransition, OperatorDecision
from cua.escalation.requests import (
    InterventionRequest,
    Queue,
    new_request_id,
    new_resume_token,
)
from cua.evidence.logger import RunLog, utc_now
from cua.replay.handoff import (
    Abort,
    Decision,
    HandBack,
    HandoffRequest,
    Ticket,
    Unanswered,
)
from cua.surface.protocol import SessionHandle
from cua.termlink import link

HUMAN_ACTIONS_FILE = "human_actions.jsonl"
WAITER_FILE = "waiter.json"
HEARTBEAT_S = 2.0
WAITER_FRESH_S = 10.0
"""A heartbeat older than this means the waiting process is gone."""


class HandoffSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    wait_s: float = Field(default=600.0, ge=0)
    """How long this process waits for someone to take the request."""
    ttl_s: float = Field(default=1800.0, gt=0)
    """How long a request stays open at all; then it is aborted."""
    operator_url: str = "http://127.0.0.1:8100"
    poll_s: float = Field(default=0.25, gt=0)
    announce: bool = False
    """Say on stderr what the process is waiting for (the CLI turns it on)."""


class OperatorChannel:
    def __init__(
        self,
        *,
        log: RunLog,
        runs_dir: Path,
        control: ControlStore,
        session: Callable[[], SessionHandle],
        kind: Literal["replay", "discovery"],
        capability: str,
        capability_version: int | None,
        tenant: str,
        settings: HandoffSettings | None = None,
    ) -> None:
        self.log = log
        self.queue = Queue(runs_dir)
        self.control = control
        self._session = session
        self.kind: Literal["replay", "discovery"] = kind
        self.capability = capability
        self.capability_version = capability_version
        self.tenant = tenant
        self.settings = settings or HandoffSettings()
        self._opened_at: dict[str, float] = {}

    # -- Handoff -------------------------------------------------------------

    def open(self, request: HandoffRequest) -> Ticket:
        request_id = new_request_id()
        token = new_resume_token()
        session = self._session()
        created = datetime.now(UTC)
        operator_url = f"{self.settings.operator_url.rstrip('/')}/requests/{request_id}"
        intervention = InterventionRequest(
            id=request_id,
            run_id=self.log.run_id,
            kind=self.kind,
            capability=self.capability,
            capability_version=self.capability_version,
            tenant=self.tenant,
            step_id=request.step_id,
            reason_code=request.reason,
            code=request.code,
            message=request.message,
            expected=request.expected,
            a11y_excerpt=request.observed,
            masked_screenshot=request.screenshot,
            side_effect=request.side_effect,
            options=request.options,
            resume_points=request.resume_points,
            attempt=request.attempt,
            cdp_url=session.cdp_url,
            target_id=session.target_id,
            page_url=session.page_url,
            devtools_url=devtools_url(session),
            operator_url=operator_url,
            created_at=_iso(created),
            expires_at=_iso(created + timedelta(seconds=self.settings.ttl_s)),
        )
        path = self.log.write_json(f"interventions/{request_id}.json", intervention)
        self.queue.post(intervention, self.log.dir, token)
        self.control.transition(
            "PAUSED",
            by="automation",
            request_id=request_id,
            note=f"{request.reason} at {request.step_id}: {request.code}",
        )
        self._opened_at[request_id] = time.monotonic()
        if self.settings.announce:
            lines = [
                f"cua: {request.reason} at {request.step_id} ({request.code}): {request.message}",
                f"cua: request {request_id} is open at {link(operator_url)}",
                f"cua: waiting up to {self.settings.wait_s:g}s for someone to take it (serve "
                "the console with `cua operator`; Ctrl+C stops waiting and leaves the session "
                "for `cua resume`)",
            ]
            print("\n".join(lines), file=sys.stderr, flush=True)
        return Ticket(
            request_id=request_id,
            resume_token=token,
            operator_url=operator_url,
            intervention=path.relative_to(self.log.dir).as_posix(),
        )

    def wait(self, ticket: Ticket) -> Decision:
        started = self._opened_at.get(ticket.request_id, time.monotonic())
        expires = _expiry(self.log.dir, ticket.request_id)
        beat = 0.0
        try:
            while True:
                record = self.control.read()
                if record.request_id == ticket.request_id and record.decision is not None:
                    if record.state == "RESUMING":
                        return _hand_back(record.decision)
                    if record.state == "ABORTED":
                        return Abort(by=record.decision.by, why=record.decision.why)
                if record.state == "ABORTED":
                    return Abort(by="unknown", why="the request was aborted")
                if expires is not None and datetime.now(UTC) >= expires:
                    try:
                        self.control.transition(
                            "ABORTED",
                            by="timeout",
                            expect_request=ticket.request_id,
                            decision=OperatorDecision(
                                kind="abort", by="timeout", why="nobody answered in time"
                            ),
                            note="the request expired",
                        )
                    except IllegalTransition:
                        continue  # answered at the last moment; look again
                    return Abort(by="timeout", why="the request expired unanswered")
                if record.state == "PAUSED" and time.monotonic() - started >= self.settings.wait_s:
                    return Unanswered(
                        why=f"nobody took the request within {self.settings.wait_s:g}s"
                    )
                if time.monotonic() - beat >= HEARTBEAT_S:
                    self._heartbeat(ticket.request_id)
                    beat = time.monotonic()
                # A plain sleep: pumping the browser's events now would let the
                # automation's own handlers answer dialogs the person raised.
                time.sleep(self.settings.poll_s)
        except KeyboardInterrupt:
            return Unanswered(why="the waiting process was interrupted")
        finally:
            _remove_heartbeat(self.log.dir / WAITER_FILE)

    def resumed(self, ticket: Ticket, *, checkpoint: str | None, next_step: str) -> None:
        where = f"{next_step}" + (f" after {checkpoint}" if checkpoint else "")
        self.control.transition(
            "AUTOMATION",
            by="automation",
            expect_request=ticket.request_id,
            note=f"resume-state search: carry on with {where}",
        )

    def human_actions(self, ticket: Ticket) -> int:
        return count_human_actions(self.log.dir, ticket.request_id)

    # -- helpers -------------------------------------------------------------

    def _heartbeat(self, request_id: str) -> None:
        try:
            (self.log.dir / WAITER_FILE).write_text(
                json.dumps({"pid": os.getpid(), "request_id": request_id, "beat": utc_now()})
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass  # the console is reading it this instant; the next beat lands


def _remove_heartbeat(path: Path) -> None:
    """Housekeeping, never a reason to fail a run. On Windows a file another
    process is reading this instant (the console asking whether anyone is
    waiting) cannot be deleted; try again briefly, then leave it: a heartbeat
    older than ``WAITER_FRESH_S`` already reads as nobody waiting."""
    for _ in range(20):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.05)


def _hand_back(decision: OperatorDecision) -> HandBack:
    return HandBack(
        by=decision.by,
        resume_at=decision.resume_at,
        retry=decision.retry,
        approved=decision.approved,
    )


def count_human_actions(run_dir: Path, request_id: str) -> int:
    """Things a person did in the browser under this request; decisions made
    on the console are in the same file and are not counted."""
    path = run_dir / HUMAN_ACTIONS_FILE
    if not path.is_file():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("request_id") == request_id and record.get("source") == "browser":
            count += 1
    return count


def waiter_alive(run_dir: Path) -> bool:
    """Is a process still waiting on this run's current request?"""
    path = run_dir / WAITER_FILE
    try:
        beat = json.loads(path.read_text(encoding="utf-8"))["beat"]
    except (OSError, ValueError, KeyError):
        return False
    age = datetime.now(UTC) - datetime.fromisoformat(str(beat).replace("Z", "+00:00"))
    return age.total_seconds() < WAITER_FRESH_S


def devtools_url(session: SessionHandle) -> str | None:
    if session.target_id is None:
        return None
    host = session.cdp_url.split("://", 1)[-1].rstrip("/")
    base = session.cdp_url.rstrip("/")
    return f"{base}/devtools/inspector.html?ws={host}/devtools/page/{session.target_id}"


def _expiry(run_dir: Path, request_id: str) -> datetime | None:
    try:
        data = json.loads((run_dir / "interventions" / f"{request_id}.json").read_text("utf-8"))
        return datetime.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError):
        return None


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")
