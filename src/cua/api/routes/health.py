"""``GET /health``: is the server up. The one endpoint that needs no key, so
it says nothing a stranger should not know."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from cua import __version__
from cua.api.app import Service
from cua.api.service import RunService

router = APIRouter(tags=["health"])


@router.get("/health")
def health(service: RunService = Service) -> dict[str, Any]:
    return {"status": "ok", "version": __version__, "runs_executing": service.busy()}
