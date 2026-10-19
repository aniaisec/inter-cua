"""What each attempt of a workflow request did, by idempotency key.

``<runs dir>/.workflows/<hash of tenant and key>.json``, beside replay's own
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

from cua.persistence import (
    PersistenceConflict,
    atomic_write_json,
    corrupt,
    file_lock,
    read_json,
    read_model,
)
from cua.replay.result import RESULT
from cua.workflow.models import WorkflowResult

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
    """Entries by tenant and key, as replay keeps its results: a key one
    tenant's caller chose is not another tenant's request."""

    def __init__(self, runs_dir: Path, tenant: str) -> None:
        self.runs_dir = runs_dir
        self.dir = runs_dir / ".workflows"
        self.tenant = tenant
        self._seen: dict[str, Entry | None] = {}

    def _path(self, key: str) -> Path:
        return self.dir / (_digest(f"{self.tenant}\n{key}") + ".json")

    def get(self, key: str) -> Entry | None:
        path = self._path(key)
        with file_lock(path):
            entry = self._read(path) if path.exists() else None
            self._seen[key] = entry.model_copy(deep=True) if entry is not None else None
            return entry if entry is not None else self._legacy(key)

    def _legacy(self, key: str) -> Entry | None:
        """An entry kept under the key alone, before entries were per tenant:
        this tenant's if its first attempt ran on this tenant. Unknown
        ownership is refused for reconciliation, never treated as a miss."""
        path = self.dir / (_digest(key) + ".json")
        if not path.exists():
            return None
        entry = self._read(path)
        first = entry.attempts[0] if entry.attempts else None
        record = self.runs_dir / "workflows" / first / "workflow.json" if first else None
        if record is None:
            raise corrupt(path)
        owner = _tenant_of(record)
        if owner is None:
            raise corrupt(record)
        if owner != self.tenant:
            return None
        return entry

    def put(self, key: str, entry: Entry) -> None:
        path = self._path(key)
        with file_lock(path):
            current = self._read(path) if path.exists() else None
            if current != self._seen.get(key):
                raise PersistenceConflict(
                    "workflow journal changed; stop and reconcile before retry"
                )
            atomic_write_json(path, entry.model_dump(mode="json"))
            self._seen[key] = entry.model_copy(deep=True)

    def _read(self, path: Path) -> Entry:
        entry = read_model(path, Entry)
        try:
            if entry.result is not None:
                WorkflowResult.model_validate(entry.result)
            for step in entry.steps.values():
                if step.result is not None:
                    RESULT.validate_python(step.result)
        except ValueError:
            raise corrupt(path) from None
        return entry


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


def find_run(runs_dir: Path, idempotency_key: str, tenant: str) -> Path | None:
    """The newest run directory that ran under this key on this tenant, if any."""
    found: list[Path] = []
    for run_json in runs_dir.glob("*/run.json"):
        data = read_json(run_json)
        request = data.get("request")
        owner = data.get("tenant")
        if not isinstance(request, dict) or not isinstance(owner, dict):
            raise corrupt(run_json)
        if request.get("idempotency_key") != idempotency_key:
            continue
        if owner.get("id") == tenant:
            found.append(run_json.parent)
    return max(found, key=lambda p: p.name) if found else None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _tenant_of(record: Path) -> str | None:
    try:
        data = read_json(record)
    except FileNotFoundError:
        return None
    tenant = data.get("tenant")
    if not isinstance(tenant, dict):
        raise corrupt(record)
    owner = tenant.get("id")
    return owner if isinstance(owner, str) else None
