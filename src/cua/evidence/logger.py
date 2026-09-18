"""One directory per run, written as the run happens.

    <run_dir>/
      run.json            what was asked: goal, params, tenant, model, limits
      log.jsonl           one event per line: what happened and why
      model_calls.jsonl   one line per model call: response id, tokens, time
      observations/       the full (scrubbed) tree behind each decision
      screenshots/        the (masked) screen behind each decision
      result.json         how it ended

Append-only JSONL so that a run that dies half way still leaves a readable
record up to the moment it died. Everything written here has already been
through redaction; this module does not know what a secret is and is not the
place to find out. What it does offer is a last pass: whoever owns the
redactor hands it over with ``scrub_with``, and from then on every string in
every line, tree and JSON file goes through it on the way to disk — a net
under the places a value reaches the log without passing a redactor first.
Screenshots are bytes and are masked when they are taken, not here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from ulid import ULID

from cua.surface.protocol import Observation

RUNS_DIR = Path("evidence/runs")


def new_run_id() -> str:
    return f"run_{ULID()}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RunLog:
    def __init__(self, run_dir: Path) -> None:
        self.dir = run_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "observations").mkdir(exist_ok=True)
        (self.dir / "screenshots").mkdir(exist_ok=True)
        self._seq = 0
        self._scrub: Callable[[str], str] | None = None

    def scrub_with(self, scrub: Callable[[str], str]) -> None:
        """Pass every string written from now on through ``scrub``."""
        self._scrub = scrub

    @classmethod
    def create(cls, root: Path = RUNS_DIR, run_id: str | None = None) -> RunLog:
        return cls(root / (run_id or new_run_id()))

    @property
    def run_id(self) -> str:
        return self.dir.name

    def event(self, kind: str, /, **fields: Any) -> None:
        self._seq += 1
        record = {"seq": self._seq, "ts": utc_now(), "event": kind, **fields}
        _append(self.dir / "log.jsonl", self._clean(record))

    def model_call(self, **fields: Any) -> None:
        _append(self.dir / "model_calls.jsonl", self._clean({"ts": utc_now(), **fields}))

    def observation(self, turn: int, observation: Observation) -> dict[str, str]:
        """Store the tree and, if taken, the screenshot. Returns their paths,
        relative to the run dir, for the log line that refers to them."""
        paths: dict[str, str] = {}
        name = f"{turn:04d}"
        tree = Path("observations") / f"{name}.json"
        _write(self.dir / tree, self._dumps(observation.model_dump(mode="json"), indent=1))
        paths["observation"] = tree.as_posix()
        if observation.screenshot_png:
            shot = Path("screenshots") / f"{name}.png"
            (self.dir / shot).write_bytes(observation.screenshot_png)
            paths["screenshot"] = shot.as_posix()
        return paths

    def write_json(self, name: str, data: BaseModel | dict[str, Any]) -> Path:
        path = self.dir / name
        raw = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
        _write(path, self._dumps(raw, indent=2))
        return path

    def _dumps(self, data: Any, *, indent: int) -> str:
        return json.dumps(self._clean(data), indent=indent, default=_default, ensure_ascii=False)

    def _clean(self, value: Any) -> Any:
        if self._scrub is None:
            return value
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        if isinstance(value, str):
            return self._scrub(value)
        if isinstance(value, dict):
            return {k: self._clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._clean(v) for v in value]
        return value


# Line endings are LF on every platform. A run directory is evidence that gets
# committed, and git stores LF: a file written with CRLF on Windows would come
# back from a clone with different bytes, and a hash taken of it would no
# longer match.


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def _append(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, default=_default) + "\n")


def _default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return value.as_posix()
    return str(value)
