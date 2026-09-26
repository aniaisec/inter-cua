"""What each attempt of a workflow request did, by idempotency key.

``<runs dir>/.workflows/<hash of key>.json``, beside replay's own
``.idempotency/``. It holds what a retry needs to be safe:

* the request's fingerprint (workflow, its content, the inputs, hashed), so
  a key reused for another request is refused;
* the versions the first attempt resolved (``pins``), so a retry runs the
  plan the first attempt ran;
* each step's last known state and run directory. A step whose attempt
  ``escalated`` is never started again by the workflow: the person, or
  ``cua resume``, finishes it in its own run directory, and the next attempt
  reads the answer there. A committing step whose attempt ``started`` and
  never reported (the process was killed) is not started again either; its
  run is looked for, and without one its side effect is ``unknown``;
* the final result, returned as is to a retry.

A file rather than a store for the reason replay's cache gives: one process
is the whole deployment here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

StepState = Literal["started", "success", "business_outcome", "failure", "escalated"]


class StepEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: StepState
    idempotency_key: str | None = None
    run_dir: str | None = None
    result: dict[str, Any] | None = None
    """The step's ReplayResult as last seen (JSON)."""


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fingerprint: str
    workflow: str
    workflow_version: int
    pins: dict[str, int]
    attempts: list[str] = Field(default_factory=list)
    """Workflow run ids, oldest first."""
    steps: dict[str, StepEntry] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    """The final WorkflowResult (JSON); never an escalated one, which is not an answer yet."""


class Journal:
    def __init__(self, runs_dir: Path) -> None:
        self.dir = runs_dir / ".workflows"

    def _path(self, key: str) -> Path:
        return self.dir / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".json")

    def get(self, key: str) -> Entry | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return Entry.model_validate_json(path.read_text(encoding="utf-8"))

    def put(self, key: str, entry: Entry) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(
            json.dumps(entry.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )


def fingerprint(workflow: str, version: int, content_sha256: str, inputs: dict[str, str]) -> str:
    canonical = json.dumps(
        {"workflow": workflow, "version": version, "content": content_sha256, "inputs": inputs},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def step_key(workflow: str, key: str, step_id: str) -> str:
    """The idempotency key a step runs under: its own per workflow request,
    so the same capability twice in one workflow is two requests, and a
    caller's own key for a single replay never collides with a step's."""
    return f"wf:{workflow}:{key}:{step_id}"


def find_run(runs_dir: Path, idempotency_key: str) -> Path | None:
    """The newest run directory that ran under this key, if any."""
    found: list[Path] = []
    for run_json in runs_dir.glob("*/run.json"):
        try:
            data = json.loads(run_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (data.get("request") or {}).get("idempotency_key") == idempotency_key:
            found.append(run_json.parent)
    return max(found, key=lambda p: p.name) if found else None
