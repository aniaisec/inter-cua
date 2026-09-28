"""The FastAPI application: identity on every request, then the routes.

Every response carries ``X-Request-Id``: the caller's, or one made up for a
GET that sent none. A POST must send its own, because a POST changes
something and the id is how the caller, this server's request log
(``<runs>/.api/requests.jsonl``) and the run record agree on which request
did it. Errors are one shape: ``{"error": {"code", "message"}, "request_id"}``.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, TypeVar

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from ulid import ULID

from cua import __version__
from cua.api.access import Caller, Gate
from cua.api.models import ApiError, ApiRun, error_body
from cua.api.service import RunService

M = TypeVar("M", bound=BaseModel)
REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def create_app(gate: Gate, service: RunService) -> FastAPI:
    from cua.api.routes import approvals, capabilities, health, runs

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        service.close()

    app = FastAPI(
        title="inter-cua capability runtime",
        version=__version__,
        description="Invoke approved capabilities over HTTP; the same ReplayResult as "
        "`cua replay`.",
        lifespan=lifespan,
    )
    app.state.gate = gate
    app.state.service = service

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message, getattr(request.state, "request_id", None)),
            status_code=exc.status,
        )

    @app.middleware("http")
    async def identify(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        given = request.headers.get("x-request-id")
        problem = None
        if given is not None and not REQUEST_ID.fullmatch(given):
            problem = "X-Request-Id is 1-128 letters, digits and . _ : - (starting alnum)"
            given = None
        elif given is None and request.method == "POST":
            problem = "a POST names itself: send an X-Request-Id header"
        request_id = given or f"req_{ULID()}"
        request.state.request_id = request_id
        if problem is not None:
            response: Response = JSONResponse(
                error_body("request_id_invalid", problem, request_id), status_code=400
            )
        else:
            response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        service.audit(
            {
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "client": getattr(request.state, "client_id", None),
                "tenant": getattr(request.state, "tenant_id", None),
            }
        )
        return response

    app.include_router(health.router)
    app.include_router(capabilities.router)
    app.include_router(runs.router)
    app.include_router(approvals.router)
    return app


# --------------------------------------------------------------------------
# Dependencies the routes share
# --------------------------------------------------------------------------


def the_service(request: Request) -> RunService:
    service: RunService = request.app.state.service
    return service


def caller(
    request: Request,
    authorization: str | None = Header(default=None),
    x_cua_tenant: str | None = Header(default=None),
) -> Caller:
    gate: Gate = request.app.state.gate
    found = gate.caller(authorization, x_cua_tenant, request.state.request_id)
    request.state.client_id = found.client_id
    request.state.tenant_id = found.tenant.id
    return found


async def json_body(request: Request) -> dict[str, Any]:
    """The body as JSON with numbers read as exact decimals, as ``cua catalog
    invoke`` reads them: ``250.00`` must reach the capability as it was sent,
    not as the nearest binary float."""
    raw = await request.body()
    try:
        data = json.loads(raw.decode("utf-8-sig") or "{}", parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(400, "invalid_json", f"the body is not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "the body must be a JSON object")
    return data


Authenticated = Depends(caller)
Service = Depends(the_service)
Body = Depends(json_body)


def parse(model: type[M], data: dict[str, Any]) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'body'}: {e['msg']}" for e in exc.errors()
        )
        raise ApiError(422, "invalid_request", problems) from None


def respond(record: ApiRun, *, replayed: bool = False) -> JSONResponse:
    """A run: 202 while a worker still holds it, 200 once it stands still."""
    headers = {"Location": f"/runs/{record.run_id}"}
    if replayed:
        headers["Idempotent-Replayed"] = "true"
    status = 202 if record.state == "running" else 200
    return JSONResponse(record.view(), status_code=status, headers=headers)
