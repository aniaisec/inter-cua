"""Intervention requests, and the file queue an operator console reads them from.

A request is everything a person needs to decide without first reconstructing
the run: which capability, which step, why it stopped, what was expected and
what the screen showed instead (a scrubbed excerpt of the accessibility tree),
a masked screenshot, and the live session to attach to (a DevTools endpoint and
the page's target id). It is written into the run directory, under
``interventions/``, through the run's own log writer — so it passes the same
scrubber as everything else the run writes — and a pointer to it is dropped in
``<runs>/.interventions/``, which is the queue.

The pointer holds only the request id, where its run lives, and the SHA-256 of
its resume token. The token itself goes to the caller (in the ``escalated``
result) and to the operator's screen, never into a file.

Why files: one machine is the whole deployment here, and a directory of small
JSON files is a queue that survives either process exiting. The first service
boundary, when there is one, is exactly this interface: post, list, find by
token.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from cua.replay.handoff import Option, ResumeChoice
from cua.replay.result import EscalationReason, SideEffect

QUEUE_DIR = ".interventions"


def new_request_id() -> str:
    return f"req_{ULID()}"


def new_resume_token() -> str:
    return "rsm_" + secrets.token_urlsafe(24)


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


class InterventionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    run_id: str
    kind: Literal["replay", "discovery"]
    capability: str
    capability_version: int | None = None
    tenant: str
    step_id: str | None
    reason_code: EscalationReason
    code: str
    """The fault underneath: LOCATOR_UNRESOLVED, POLICY_BLOCKED, APP_ERROR, ..."""
    message: str
    expected: str = ""
    a11y_excerpt: str = ""
    masked_screenshot: str | None = None
    """Relative to the run directory."""
    side_effect: SideEffect = "none"
    options: list[Option]
    resume_points: list[ResumeChoice] = Field(default_factory=list)
    attempt: int = 1
    cdp_url: str
    target_id: str | None = None
    """Which page of the browser the run is driving."""
    page_url: str = ""
    devtools_url: str | None = None
    """The page in the browser's own DevTools front end: a way to see and
    drive a headless session from any Chromium on the same machine."""
    operator_url: str | None = None
    created_at: str
    expires_at: str


class QueueEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    run_dir: str
    """Relative to the runs directory when the run lives under it."""
    token_sha256: str
    created_at: str


class Queue:
    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir
        self.dir = runs_dir / QUEUE_DIR

    def post(self, request: InterventionRequest, run_dir: Path, resume_token: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            where = run_dir.resolve().relative_to(self.runs_dir.resolve()).as_posix()
        except ValueError:
            where = run_dir.resolve().as_posix()
        entry = QueueEntry(
            request_id=request.id,
            run_dir=where,
            token_sha256=token_sha256(resume_token),
            created_at=request.created_at,
        )
        (self.dir / f"{request.id}.json").write_text(
            entry.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n"
        )

    def entries(self) -> list[QueueEntry]:
        if not self.dir.is_dir():
            return []
        out = []
        for path in sorted(self.dir.glob("req_*.json"), reverse=True):
            try:
                out.append(QueueEntry.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    def entry(self, request_id: str) -> QueueEntry | None:
        path = self.dir / f"{Path(request_id).name}.json"
        if not path.is_file():
            return None
        return QueueEntry.model_validate_json(path.read_text(encoding="utf-8"))

    def by_token(self, token: str) -> QueueEntry | None:
        wanted = token_sha256(token)
        return next((e for e in self.entries() if e.token_sha256 == wanted), None)

    def run_dir(self, entry: QueueEntry) -> Path:
        path = Path(entry.run_dir)
        return path if path.is_absolute() else self.runs_dir / path

    def request(self, entry: QueueEntry) -> InterventionRequest:
        path = self.run_dir(entry) / "interventions" / f"{entry.request_id}.json"
        return InterventionRequest.model_validate(json.loads(path.read_text(encoding="utf-8")))
