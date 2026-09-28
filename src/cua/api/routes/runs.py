"""Runs: start one, read it, read its events, resume or abort it.

``?wait=S`` on ``POST /runs``, ``GET /runs/{id}`` and the actions on a run
holds the answer up to ``S`` seconds (at most 300) for the run to stand
still. Without it the answer is immediate: ``202`` while the run executes,
``200`` once it is ``escalated``, ``finished``, ``error`` or ``lost``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from cua.api.access import Caller
from cua.api.app import Authenticated, Body, Service, parse, respond
from cua.api.models import AbortRequest, ResumeRequest, RunRequest
from cua.api.service import MAX_WAIT_S, RunService

router = APIRouter(tags=["runs"])

Wait = Query(default=0.0, ge=0, le=MAX_WAIT_S, description="Seconds to wait for the run")


@router.post("/runs")
def start_run(
    data: dict[str, Any] = Body,
    wait: float = Wait,
    idempotency_key: str | None = Header(default=None),
    caller: Caller = Authenticated,
    service: RunService = Service,
) -> JSONResponse:
    body = parse(RunRequest, data)
    record, replayed = service.submit(caller, body, idempotency_key)
    service.wait(record.run_id, wait)
    return respond(service.get(caller, record.run_id), replayed=replayed)


@router.get("/runs/{run_id}")
def get_run(
    run_id: str, wait: float = Wait, caller: Caller = Authenticated, service: RunService = Service
) -> JSONResponse:
    caller.require("read")
    service.get(caller, run_id)
    service.wait(run_id, wait)
    return respond(service.get(caller, run_id))


@router.get("/runs/{run_id}/events")
def get_events(
    run_id: str, caller: Caller = Authenticated, service: RunService = Service
) -> dict[str, Any]:
    caller.require("read")
    events, warnings = service.events(caller, run_id)
    return {"run_id": run_id, "events": events, "warnings": warnings}


@router.post("/runs/{run_id}/resume")
def resume_run(
    run_id: str,
    data: dict[str, Any] = Body,
    wait: float = Wait,
    caller: Caller = Authenticated,
    service: RunService = Service,
) -> JSONResponse:
    service.resume(caller, run_id, parse(ResumeRequest, data))
    service.wait(run_id, wait)
    return respond(service.get(caller, run_id))


@router.post("/runs/{run_id}/abort")
def abort_run(
    run_id: str,
    data: dict[str, Any] = Body,
    caller: Caller = Authenticated,
    service: RunService = Service,
) -> JSONResponse:
    return respond(service.abort(caller, run_id, parse(AbortRequest, data)))
