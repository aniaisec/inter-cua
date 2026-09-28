"""``POST /runs/{id}/approve``: consent for the commit an escalated run
stopped at.

The body carries a signed approval token (``cua approval-token``), checked as
``cua resume --approval-token`` checks it: this capability, at this content,
on this tenant, with these inputs, unexpired and unspent. A refused token is
``403`` and leaves the run waiting exactly as it was.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from cua.api.access import Caller
from cua.api.app import Authenticated, Body, Service, parse, respond
from cua.api.models import ApproveRequest
from cua.api.routes.runs import Wait
from cua.api.service import RunService

router = APIRouter(tags=["approvals"])


@router.post("/runs/{run_id}/approve")
def approve_run(
    run_id: str,
    data: dict[str, Any] = Body,
    wait: float = Wait,
    caller: Caller = Authenticated,
    service: RunService = Service,
) -> JSONResponse:
    service.approve(caller, run_id, parse(ApproveRequest, data))
    service.wait(run_id, wait)
    return respond(service.get(caller, run_id))
