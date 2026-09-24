"""Raw benchmark records: one JSON line per invocation, one per session.

Append-only. Aggregates are always recomputed from these, so a new question
about old runs needs no re-run.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from cua.benchmark.models import RunMetrics

REPORTS_DIR = Path("bench/reports")
RUNS_FILE = "runs.jsonl"
SESSIONS_FILE = "sessions.jsonl"


class SessionInfo(BaseModel):
    """What was run, and on what: enough to say whether two sessions compare."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    started_at: str
    finished_at: str | None = None
    suite: str
    tasks: list[str]
    strategies: list[str]
    repetitions: int | None = None
    """The override given on the command line, if any; else each task's own."""
    llm: str
    """What the baseline and discovery ran on: ``scripted``, or the provider."""
    model: str | None = None
    git_commit: str | None = None
    git_dirty: bool | None = None
    python: str
    platform: str
    playwright: str | None = None
    browser: str | None = None
    pricing_sha256: str | None = None
    runs: int = 0
    notes: list[str] = Field(default_factory=list)


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def append_run(row: RunMetrics, root: Path = REPORTS_DIR) -> None:
    _append(root / RUNS_FILE, row.model_dump_json())


def append_session(info: SessionInfo, root: Path = REPORTS_DIR) -> None:
    _append(root / SESSIONS_FILE, info.model_dump_json())


def read_runs(root: Path = REPORTS_DIR, sessions: Iterable[str] = ()) -> list[RunMetrics]:
    wanted = set(sessions)
    path = root / RUNS_FILE
    if not path.is_file():
        return []
    rows = [
        RunMetrics.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [r for r in rows if not wanted or r.session_id in wanted]


def read_sessions(root: Path = REPORTS_DIR) -> list[SessionInfo]:
    """The last record per session wins: a session is written when it starts
    and again, complete, when it ends."""
    path = root / SESSIONS_FILE
    if not path.is_file():
        return []
    latest: dict[str, SessionInfo] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            info = SessionInfo.model_validate(json.loads(line))
            latest[info.session_id] = info
    return list(latest.values())
