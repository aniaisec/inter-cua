"""Runs over HTTP: started in a worker, recorded on disk, answered by id.

A replay takes seconds, and one handed to a person can take an hour, so
``POST /runs`` does not hold the connection for it: the run gets its id at
once (the id its run directory will have), a worker thread executes it with
``cua.replay.runner`` exactly as ``cua replay`` would, and the caller reads
``GET /runs/{id}`` — or asks the POST to wait a few seconds for the answer.

What is recorded, under ``<runs dir>/.api/``:

* ``runs/<run id>.json`` — the ``ApiRun``: who asked, for which tenant, under
  which request id, and the ``ReplayResult`` once there is one;
* ``idempotency/<hash>.json`` — which run an idempotency key started, and for
  what request. Checked/published under a file lock with atomic replacement,
  so a competing reader cannot see a half-written claim;
* ``requests.jsonl`` — every request the API answered (``cua.api.app``).

Idempotency keys are the caller's, scoped to the client and tenant: the key
the replay runner sees (and caches results under) is
``api:<tenant>:<client>:<key>``, so two clients choosing the same key cannot
be answered with each other's result. The result a caller gets back carries
its own key, as it sent it.

The run directory stays the record of what happened. A run the API finds in
an unexpected state — handed back or aborted on the operator console, carried
on by ``cua resume``, or cut off by this server stopping — is read from its
run directory whenever it is asked for, never assumed.

One server per runs directory: which runs are executing is known to this
process only.
"""

from __future__ import annotations

import gc
import hashlib
import json
import math
import platform
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from cua import catalog
from cua.api.access import Caller
from cua.api.models import (
    AbortRequest,
    ApiError,
    ApiRun,
    ApproveRequest,
    ResumeRequest,
    RunRequest,
)
from cua.artifact.store import ArtifactError, open_capability
from cua.escalation.channel import HandoffSettings
from cua.escalation.requests import Queue
from cua.evidence.logger import append_jsonl, new_run_id, utc_now
from cua.observability.recorder import is_run_dir, read_run
from cua.persistence import atomic_write_json, corrupt, file_lock, read_json, read_jsonl, read_model
from cua.project import ProjectContext
from cua.replay import runner
from cua.replay.engine import ReplayConfig
from cua.replay.invocation import ApprovalGrant, Invocation
from cua.replay.result import RESULT, Failure, ReplayResult, to_json
from cua.replay.runner import InvocationError

API_DIR = ".api"
MAX_WAIT_S = 300.0


@dataclass(frozen=True)
class ServiceSettings:
    runs_dir: Path
    capabilities_dir: Path = Path("capabilities")
    operator_url: str = "http://127.0.0.1:8100"
    """Where intervention requests say the operator console is."""
    workers: int = 2
    """Maximum simultaneous executions."""
    allow_inject: bool = False
    project: ProjectContext | None = None
    max_pending_runs: int = 32
    """Additional accepted runs beyond the worker slots."""
    max_mcp_calls: int = 8
    """Outstanding stdio MCP work calls; immediate status polls use the reader."""
    queue_timeout_s: float = 300.0
    """Independent queue deadline; the invocation budget begins at dispatch."""

    def __post_init__(self) -> None:
        for name, minimum in (("workers", 1), ("max_pending_runs", 0), ("max_mcp_calls", 1)):
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer at least {minimum}")
        if (
            isinstance(self.queue_timeout_s, bool)
            or not math.isfinite(self.queue_timeout_s)
            or not 0.001 <= self.queue_timeout_s <= 86400
        ):
            raise ValueError("queue_timeout_s must be finite and between 0.001 and 86400 seconds")
        # Workers receive absolute paths even when the library caller used cwd defaults.
        runs = self.project.relative(self.runs_dir) if self.project else self.runs_dir.resolve()
        caps = self.capabilities_dir
        if self.project is not None:
            caps = (
                self.project.path("capabilities")
                if caps == Path("capabilities")
                else (self.project.relative(caps))
            )
        object.__setattr__(self, "runs_dir", runs)
        object.__setattr__(self, "capabilities_dir", caps.resolve())


class RunService:
    def __init__(
        self,
        settings: ServiceSettings,
        *,
        replay: Callable[..., ReplayResult] = runner.replay,
        resume: Callable[..., ReplayResult] = runner.resume,
        check_resume: Callable[..., ReplayResult | None] = runner.check_resume,
        environ: dict[str, str] | None = None,
    ) -> None:
        """``replay``, ``resume`` and ``check_resume`` are the runner's; a test
        hands in its own to exercise the API without a browser."""
        self.settings = settings
        self.root = settings.runs_dir / API_DIR
        self._replay = replay
        self._resume = resume
        self._check_resume = check_resume
        self._environ = (
            dict(environ)
            if environ is not None
            else (dict(settings.project.environ) if settings.project is not None else None)
        )
        self._pool = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="cua-run")
        self._active: dict[str, Future[None]] = {}
        self._running: set[str] = set()
        self._accepting = True
        self._lock = threading.RLock()

    def close(self) -> None:
        """Let executing runs finish: stopping one mid-step is how a commit
        ends up ``unknown``."""
        try:
            with self._lock:
                self._accepting = False
                for run_id in self._active:
                    record = self._load(run_id)
                    if record is not None and record.state == "queued":
                        self._cancel_queued(record, "service shutdown cancelled waiting work")
        finally:
            self._pool.shutdown(wait=True)

    def busy(self) -> int:
        with self._lock:
            return len(self._running)

    def metrics(self, caller: Caller) -> dict[str, int]:
        """Aggregate only the caller's visible records; no keys or run labels."""
        from cua.maintenance import safe_path

        caller.require("read")
        caller.require("operate")
        counts = dict.fromkeys(
            (
                "queued",
                "running",
                "escalated",
                "queue_time_ms",
                "queue_samples",
                "oldest_queue_ms",
                "unknown_side_effects",
                "storage_bytes",
                "expired_sessions",
            ),
            0,
        )
        with self._lock:
            for path in (self.root / "runs").glob("*.json"):
                if not safe_path(self.settings.runs_dir, path):
                    continue
                record = read_model(path, ApiRun)
                if (
                    record.tenant != caller.tenant.id
                    or record.client != caller.client_id
                    or not caller.client.may_use(record.capability)
                ):
                    continue
                if record.state in ("queued", "running", "escalated"):
                    counts[record.state] += 1
                if record.state == "queued" and record.queued_at:
                    age = datetime.fromisoformat(utc_now()) - datetime.fromisoformat(
                        record.queued_at
                    )
                    counts["oldest_queue_ms"] = max(
                        counts["oldest_queue_ms"], max(0, int(age.total_seconds() * 1000))
                    )
                if record.queue_duration_ms is not None:
                    counts["queue_time_ms"] += record.queue_duration_ms
                    counts["queue_samples"] += 1
                if record.result and record.result.get("side_effect") == "unknown":
                    counts["unknown_side_effects"] += 1
                run = self.settings.runs_dir / record.run_id
                if safe_path(self.settings.runs_dir, run):
                    if record.state == "escalated":
                        from cua.escalation.requests import InterventionRequest

                        for request in (run / "interventions").glob("*.json"):
                            if safe_path(self.settings.runs_dir, request):
                                intervention = read_model(request, InterventionRequest)
                                if datetime.fromisoformat(
                                    intervention.expires_at
                                ) < datetime.fromisoformat(utc_now()):
                                    counts["expired_sessions"] += 1
                    for item in run.rglob("*"):
                        if safe_path(self.settings.runs_dir, item) and item.is_file():
                            counts["storage_bytes"] += item.stat().st_size
        return counts

    def _admit(self) -> None:
        if not self._accepting:
            raise ApiError(503, "service_stopping", "service is shutting down", retry_after_s=1)
        if len(self._active) >= self.settings.workers + self.settings.max_pending_runs:
            raise ApiError(
                503,
                "server_busy",
                "run capacity is exhausted; retry with the same key",
                retry_after_s=1,
            )

    def _queue(self, record: ApiRun) -> None:
        now = utc_now()
        record.state = "queued"
        record.queued_at = record.updated_at = now
        record.queue_expires_at = (
            (datetime.fromisoformat(now) + timedelta(seconds=self.settings.queue_timeout_s))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        record.started_at = None
        record.queue_duration_ms = None

    def _queue_time(self, record: ApiRun) -> None:
        if record.queued_at is not None:
            elapsed = datetime.fromisoformat(utc_now()) - datetime.fromisoformat(record.queued_at)
            record.queue_duration_ms = max(0, int(elapsed.total_seconds() * 1000))

    def _queue_expired(self, record: ApiRun) -> bool:
        return record.queue_expires_at is not None and (
            datetime.fromisoformat(utc_now()) >= datetime.fromisoformat(record.queue_expires_at)
        )

    def _cancel_queued(self, record: ApiRun, why: str, *, expired: bool = False) -> None:
        self._queue_time(record)
        record.error = why
        if record.result is not None:
            # A resume has not touched its browser yet. Keep the live escalation
            # available instead of fabricating a terminal answer for that run.
            record.state = "escalated"
            record.updated_at = utc_now()
            self._save(record)
        else:
            if record.version is None:
                raise corrupt(self._path(record.run_id))
            self._settle(
                record,
                Failure(
                    code="TIMEOUT" if expired else "INTERRUPTED",
                    message=why,
                    capability=record.capability,
                    capability_version=record.version,
                    run_id=record.run_id,
                    idempotency_key=record.idempotency_key,
                    side_effect="none",
                ),
            )

    # -- starting a run --------------------------------------------------------

    def submit(
        self, caller: Caller, body: RunRequest, idempotency_key: str | None
    ) -> tuple[ApiRun, bool]:
        """The run this request started, and whether it is one an earlier
        request with the same idempotency key started instead."""
        caller.require("invoke")
        if body.approval is not None:
            caller.require("approve")
        caller.require_capability(body.capability)
        if body.inject is not None and not self.settings.allow_inject:
            raise ApiError(
                400, "inject_not_allowed", "this server was not started with --allow-inject"
            )
        with self._lock:
            self.check_audit()
            if idempotency_key is not None:
                earlier = self._claim(caller, idempotency_key, _fingerprint(body), "", create=False)
                if earlier is not None:
                    return self.get(caller, earlier), True
        try:
            path = catalog.find(
                self.settings.capabilities_dir,
                body.capability,
                body.version,
                project=self.settings.project,
            )
            cap = open_capability(path).capability
        except catalog.CatalogError as exc:
            raise ApiError(404, "capability_not_found", str(exc)) from None
        except ArtifactError as exc:
            raise ApiError(409, "capability_unreadable", str(exc)) from None
        c = cap.contract
        if idempotency_key is None and (c.side_effects != "none" or not c.idempotent):
            raise ApiError(
                428,
                "idempotency_key_required",
                f"{cap.name} is not read-only; send an Idempotency-Key header so that a "
                "retry after a lost answer returns this run instead of starting another",
            )
        inputs, problems = catalog.coerce(cap, body.inputs)
        now = utc_now()
        record = ApiRun(
            run_id=new_run_id(),
            state="running",
            capability=cap.name,
            version=cap.version,
            tenant=caller.tenant.id,
            client=caller.client_id,
            request_id=caller.request_id,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
            requests=[caller.request_id],
        )
        with self._lock:
            self.check_audit()
            if idempotency_key is not None:
                earlier = self._claim(caller, idempotency_key, _fingerprint(body), "", create=False)
                if earlier is not None:
                    return self.get(caller, earlier), True
            self._admit()
            if problems:
                failure = catalog.input_invalid(cap, problems, idempotency_key)
                self._settle(record, failure)
            else:
                self._queue(record)
                self._save(record)
            # Publish a complete run before its claim: a crash or failed write
            # can leave an undispatched record, never a claim pointing at nothing.
            if idempotency_key is not None:
                earlier = self._claim(caller, idempotency_key, _fingerprint(body), record.run_id)
                if earlier is not None:
                    return self.get(caller, earlier), True
            if problems:
                return record, False
            invocation = Invocation(
                inputs=inputs,
                idempotency_key=_scoped(caller, idempotency_key),
                approval=body.approval,
                budget=body.budget,
                inject=body.inject,
            )
            self._start(record, lambda: self._first_run(record, path, caller, invocation, body))
        return record, False

    def _first_run(
        self, record: ApiRun, path: Path, caller: Caller, invocation: Invocation, body: RunRequest
    ) -> None:
        handoff = (
            HandoffSettings(
                wait_s=0.0, ttl_s=body.handoff.ttl_s, operator_url=self.settings.operator_url
            )
            if body.handoff is not None
            else None
        )
        self._carry(
            record,
            lambda: self._replay(
                path,
                tenant=caller.tenant,
                policy=caller.policy,
                invocation=invocation,
                runs_dir=self.settings.runs_dir,
                config=ReplayConfig(
                    screenshots=None if body.screenshots else False, vision=body.vision
                ),
                environ=self._environ,
                handoff=handoff,
                run_id=record.run_id,
            ),
        )

    # -- reading ---------------------------------------------------------------

    def get(self, caller: Caller, run_id: str) -> ApiRun:
        """The run, as it stands now. Another client's, or another tenant's,
        is not found: whether it exists is not this caller's to learn.

        Read under the lock: a worker settling the run in between would
        otherwise be overwritten by the copy read before it."""
        with self._lock:
            record = self._load(run_id)
            if (
                record is None
                or record.client != caller.client_id
                or record.tenant != caller.tenant.id
            ):
                raise ApiError(404, "run_not_found", f"no run {run_id}")
            return self._refresh(record)

    def wait(self, run_id: str, seconds: float) -> None:
        with self._lock:
            future = self._active.get(run_id)
        if future is not None and seconds > 0:
            wait_for([future], timeout=min(seconds, MAX_WAIT_S))

    def events(self, caller: Caller, run_id: str) -> tuple[list[dict[str, Any]], list[str]]:
        """The run's canonical events (``cua.observability``) and any lines
        that could not be read. A run refused before it started has none."""
        self.get(caller, run_id)
        run_dir = self.settings.runs_dir / run_id
        if not is_run_dir(run_dir):
            return [], []
        read = read_run(run_dir)
        return [e.model_dump(mode="json") for e in read.events], read.warnings

    # -- an escalated run ------------------------------------------------------

    def approve(self, caller: Caller, run_id: str, body: ApproveRequest) -> ApiRun:
        caller.require("approve")
        record = self._escalated(caller, run_id)
        result = record.result or {}
        if result.get("reason") != "NEEDS_APPROVAL":
            raise ApiError(
                409,
                "approval_not_asked",
                f"run {run_id} is escalated for {result.get('reason')}, not NEEDS_APPROVAL; "
                "a person decides it on the operator console, then resume it",
            )
        grant = ApprovalGrant(token=body.token, approved_by=body.approved_by)
        return self._continue(caller, record, inputs=body.inputs, approval=grant, resume_at=None)

    def resume(self, caller: Caller, run_id: str, body: ResumeRequest) -> ApiRun:
        caller.require("operate")
        record = self._escalated(caller, run_id)
        return self._continue(
            caller, record, inputs=body.inputs, approval=None, resume_at=body.resume_at
        )

    def abort(self, caller: Caller, run_id: str, body: AbortRequest) -> ApiRun:
        """End an escalated run as ``ESCALATION_ABORTED``, as the operator
        console's Abort does: the session is closed and the side effect is
        what the run can vouch for."""
        from cua.escalation.operator_app import Console, ConsoleError

        caller.require("operate")
        with self._lock:
            record = self._escalated(caller, run_id)
            request_id = (record.result or {}).get("request_id")
            if not isinstance(request_id, str):
                raise ApiError(409, "run_not_escalated", f"run {run_id} has no open request")
            console = Console(self.settings.runs_dir, policy=caller.policy, tenant=caller.tenant.id)
            try:
                console.act(request_id, "abort", by=_by(caller), why=body.why)
            except ConsoleError as exc:
                raise ApiError(exc.status, "abort_refused", str(exc)) from None
            record.requests.append(caller.request_id)
            self._save(record)
            return self._refresh(record)

    def _escalated(self, caller: Caller, run_id: str) -> ApiRun:
        record = self.get(caller, run_id)
        caller.require_capability(record.capability)
        if record.state != "escalated":
            raise ApiError(
                409, "run_not_escalated", f"run {run_id} is {record.state}, not escalated"
            )
        return record

    def _continue(
        self,
        caller: Caller,
        record: ApiRun,
        *,
        inputs: dict[str, str],
        approval: ApprovalGrant | None,
        resume_at: str | None,
    ) -> ApiRun:
        token = (record.result or {}).get("resume_token")
        if not isinstance(token, str):
            raise ApiError(409, "run_not_escalated", f"run {record.run_id} has no resume token")
        runs_dir = self.settings.runs_dir
        with self._lock:
            if record.run_id in self._active:
                raise ApiError(409, "run_busy", f"run {record.run_id} is already executing")
            record.requests.append(caller.request_id)
            try:
                answered = self._check_resume(
                    token,
                    runs_dir=runs_dir,
                    inputs=inputs,
                    approval=approval,
                    environ=self._environ,
                )
            except InvocationError as exc:
                # The run was not touched: it still waits, to be resumed again.
                record.error = str(exc)
                self._save(record)
                refused = str(exc).startswith("approval refused")
                raise ApiError(
                    403 if refused else 409,
                    "approval_refused" if refused else "resume_refused",
                    str(exc),
                ) from None
            if answered is not None:
                self._settle(record, answered)
                return record
            self._admit()
            self._queue(record)
            record.error = None
            self._save(record)
            self._start(
                record,
                lambda: self._carry(
                    record,
                    lambda: self._resume(
                        token,
                        runs_dir=runs_dir,
                        by=_by(caller),
                        resume_at=resume_at,
                        inputs=inputs,
                        approval=approval,
                        environ=self._environ,
                        wait_s=0.0,
                    ),
                ),
            )
        return record

    # -- the workers -----------------------------------------------------------

    def _start(self, record: ApiRun, work: Callable[[], None]) -> None:
        run_id = record.run_id

        def run() -> None:
            com = None
            try:
                with self._lock:
                    current = self._load(run_id)
                    if current is None or current.state != "queued":
                        return
                    if not self._accepting:
                        self._cancel_queued(record, "service shutdown cancelled waiting work")
                        return
                    if self._queue_expired(record):
                        self._cancel_queued(record, "queue expired before execution", expired=True)
                        return
                    self._queue_time(record)
                    record.state = "running"
                    record.started_at = record.updated_at = utc_now()
                    self._save(record)
                    self._running.add(run_id)
                com = _com_enter()
                work()
            finally:
                _com_leave(com)
                with self._lock:
                    self._active.pop(run_id, None)
                    self._running.discard(run_id)

        with self._lock:
            # _admit ran under this same lock before any durable claim. The
            # executor therefore holds at most workers + max_pending_runs jobs.
            self._active[run_id] = self._pool.submit(run)

    def _carry(self, record: ApiRun, execute: Callable[[], ReplayResult]) -> None:
        """Execute, and record whatever came of it. A request that could not be
        made at all is ``error``, as ``cua replay`` exits 64; an exception the
        engine wrote a result for (``INTERRUPTED``) is that result."""
        try:
            result = execute()
        except (InvocationError, OSError, ValueError, ValidationError) as exc:
            self._broken(record, str(exc))
            return
        except Exception as exc:
            self._broken(record, f"{type(exc).__name__}: {exc}")
            return
        with self._lock:
            self._settle(record, result)

    def _broken(self, record: ApiRun, why: str) -> None:
        with self._lock:
            record.error = why
            stored = self._stored(record.run_id)
            if stored is not None:
                self._settle(record, stored)
                return
            if record.state == "running" and record.result is not None:
                # A resume that was refused after all: the run still waits.
                record.state = "escalated"
            elif record.state == "running":
                record.state = "error"
            record.updated_at = utc_now()
            self._save(record)

    def _settle(self, record: ApiRun, result: ReplayResult) -> None:
        record.result = _answer(record, result)
        record.state = "escalated" if result.kind == "escalated" else "finished"
        record.version = result.capability_version
        record.updated_at = utc_now()
        self._save(record)

    def _refresh(self, record: ApiRun) -> ApiRun:
        """What the run directory says now, for a run no worker holds."""
        with self._lock:
            if record.state == "queued":
                if record.run_id not in self._active:
                    self._cancel_queued(record, "service stopped before queued work dispatched")
                elif self._queue_expired(record):
                    self._cancel_queued(record, "queue expired before execution", expired=True)
                else:
                    return record
                # A cancelled resume returns to escalation. Run its normal TTL
                # and terminal-result checks too, especially after a restart.
                return self._refresh(record)
            if record.run_id in self._active or record.state in ("finished", "error"):
                return record
            if record.state == "escalated":
                self._expire(record)
            stored = self._stored(record.run_id)
            if stored is not None:
                if _answer(record, stored) != record.result:
                    self._settle(record, stored)
            elif record.state == "running":
                record.state = "lost"
                record.error = (
                    "the server stopped while this run was executing, and its run directory "
                    "holds no result; read its evidence before retrying"
                )
                record.updated_at = utc_now()
                self._save(record)
            return record

    def _expire(self, record: ApiRun) -> None:
        """End an escalated run whose request has outlived its time to live,
        as the operator console does when it is looked at: nobody is waiting
        on it, so reading it is the moment to notice."""
        from cua.escalation.operator_app import Console

        request_id = (record.result or {}).get("request_id")
        queue = Queue(self.settings.runs_dir)
        entry = queue.entry(request_id) if isinstance(request_id, str) else None
        if entry is None:
            return
        expires_at = queue.request(entry).expires_at
        if expires_at <= utc_now():
            Console(self.settings.runs_dir).sweep()

    # -- files -------------------------------------------------------------------

    def _path(self, run_id: str) -> Path:
        if not run_id.replace("_", "").isalnum():
            raise ApiError(404, "run_not_found", f"no run {run_id}")
        return self.root / "runs" / f"{run_id}.json"

    def _load(self, run_id: str) -> ApiRun | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        record = read_model(path, ApiRun)
        try:
            result = RESULT.validate_python(record.result) if record.result is not None else None
        except ValueError:
            raise corrupt(path) from None
        if (
            record.run_id != run_id
            or (record.state in ("finished", "escalated") and result is None)
            or (record.state == "escalated" and result is not None and result.kind != "escalated")
        ):
            raise corrupt(path)
        if record.state == "queued":
            try:
                if record.queued_at is None or record.queue_expires_at is None:
                    raise ValueError
                if record.started_at is not None or record.version is None:
                    raise ValueError
                queued = datetime.fromisoformat(record.queued_at)
                expires = datetime.fromisoformat(record.queue_expires_at)
                if queued.utcoffset() != timedelta(0) or expires.utcoffset() != timedelta(0):
                    raise ValueError
                if expires <= queued:
                    raise ValueError
                if result is not None and result.kind != "escalated":
                    raise ValueError
            except (ValueError, TypeError):
                raise corrupt(path) from None
        return record

    def _save(self, record: ApiRun) -> None:
        path = self._path(record.run_id)
        with file_lock(path):
            if path.exists():
                self._load(record.run_id)  # preserve corrupt originals for reconciliation
            atomic_write_json(path, record.model_dump(mode="json"))

    def _stored(self, run_id: str) -> ReplayResult | None:
        path = self.settings.runs_dir / run_id / "result.json"
        if not path.exists():
            return None
        try:
            return RESULT.validate_python(read_json(path))
        except ValueError:
            raise corrupt(path) from None

    def _claim(
        self, caller: Caller, key: str, fingerprint: str, run_id: str, *, create: bool = True
    ) -> str | None:
        """Record that ``key`` starts ``run_id``; or the run it already started."""
        scope = f"{caller.client_id}\n{caller.tenant.id}\n{key}"
        path = (
            self.root / "idempotency" / (hashlib.sha256(scope.encode()).hexdigest()[:32] + ".json")
        )
        if not create and not path.exists():
            return None
        record = {"run_id": run_id, "fingerprint": fingerprint, "at": utc_now()}
        with file_lock(path):
            if not path.exists():
                if not create:
                    return None
                atomic_write_json(path, record)
                return None
            earlier = read_json(path)
        if (
            not isinstance(earlier.get("fingerprint"), str)
            or not isinstance(earlier.get("run_id"), str)
            or not earlier["run_id"]
            or not earlier["run_id"].replace("_", "").isalnum()
        ):
            raise corrupt(path)
        if earlier["fingerprint"] != fingerprint:
            raise ApiError(
                409,
                "idempotency_conflict",
                f"idempotency key {key!r} was already used for a different request "
                "(another capability, version or inputs)",
            )
        owned = self._load(earlier["run_id"])
        if owned is None or owned.tenant != caller.tenant.id or owned.client != caller.client_id:
            raise corrupt(path)
        return str(earlier["run_id"])

    def audit(self, record: dict[str, Any]) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            append_jsonl(self.root / "requests.jsonl", {"ts": utc_now(), **record})

    def check_audit(self) -> None:
        path = self.root / "requests.jsonl"
        with file_lock(path):
            read_jsonl(path)


def _answer(record: ApiRun, result: ReplayResult) -> dict[str, Any]:
    """The result as ``cua replay`` prints it, with the caller's own key."""
    answer = result.model_copy(update={"idempotency_key": record.idempotency_key})
    data: dict[str, Any] = json.loads(to_json(answer))
    return data


def _fingerprint(body: RunRequest) -> str:
    """What makes two requests with one key "the same request": the
    capability, the version asked for, and the inputs as sent. Hashed, so a
    sensitive input is never kept."""
    canonical = json.dumps(
        {"capability": body.capability, "version": body.version, "inputs": body.inputs},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _scoped(caller: Caller, key: str | None) -> str | None:
    return None if key is None else f"api:{caller.tenant.id}:{caller.client_id}:{key}"


def _by(caller: Caller) -> str:
    return f"api:{caller.client_id}"


def _com_enter() -> Any:
    """A run may drive a desktop application, and UI Automation is COM, which
    each thread initialises for itself. Initialised per run, not per worker
    thread, so that it is also uninitialised on the thread that initialised
    it, when the run is over. The COM module, or None off Windows or without
    the windows extra (a desktop run is then refused on its own terms)."""
    if platform.system() != "Windows":
        return None
    try:
        import comtypes

        comtypes.CoInitializeEx()
    except Exception:
        return None
    return comtypes


def _com_leave(com: Any) -> None:
    # The run's UI Automation objects belong to this thread's apartment:
    # released here, before it closes. Left to a later collection on another
    # thread, releasing them raises RPC_E_DISCONNECTED.
    gc.collect()
    if com is not None:
        com.CoUninitialize()
