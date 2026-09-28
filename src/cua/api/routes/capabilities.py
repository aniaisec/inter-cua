"""The capabilities a caller may run on its tenant.

A view of the capability registry, like ``cua catalog``: by default only what
unattended replay would accept (approved, on a surface this build can drive),
for the tenant's application family, and only the capabilities the client is
authorized for. ``?all=true`` adds drafts and retired versions, marked.

A capability is described, never handed over: the tool definition, the
contract and the lifecycle, and no credential references, locators or paths.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from cua import catalog
from cua.api.access import Caller
from cua.api.app import Authenticated, Service
from cua.api.models import ApiError
from cua.api.service import RunService
from cua.artifact import requirements
from cua.registry import lifecycle
from cua.registry.models import Version
from cua.registry.resolver import default_version
from cua.registry.store import Registry

router = APIRouter(tags=["capabilities"])


@router.get("/capabilities")
def list_capabilities(
    all: bool = False, caller: Caller = Authenticated, service: RunService = Service
) -> dict[str, Any]:
    caller.require("read")
    entries, _ = catalog.scan(service.settings.capabilities_dir)
    shown = [
        e
        for e in entries
        if caller.client.may_use(e.capability.name)
        and e.capability.target.app_family == caller.tenant.app_family
        and (all or e.invocable)
    ]
    return {
        "tenant": caller.tenant.id,
        "capabilities": [
            _summary(e.capability.name, e.capability.version, e.status, e.versions, e)
            for e in shown
        ],
    }


@router.get("/capabilities/{name}")
def get_capability(
    name: str, caller: Caller = Authenticated, service: RunService = Service
) -> dict[str, Any]:
    versions = _versions(caller, service, name)
    return _detail(default_version(versions), len(versions))


@router.get("/capabilities/{name}/versions")
def get_versions(
    name: str, caller: Caller = Authenticated, service: RunService = Service
) -> dict[str, Any]:
    versions = _versions(caller, service, name)
    chosen = default_version(versions).record.version
    return {
        "name": name,
        "default": chosen,
        "versions": [
            {
                "version": v.record.version,
                "status": v.status,
                "invocable": _invocable(v),
                "default": v.record.version == chosen,
                "artifact_hash": v.record.artifact_hash,
                "created_at": v.record.created_at,
                "approved_at": v.record.approved_at,
                "approved_by": v.record.approved_by,
                "registered": v.record.registered,
                "history": [
                    {
                        "status": h.status,
                        "previous": h.previous,
                        "by": h.by,
                        "at": h.at,
                        "reason": h.reason,
                    }
                    for h in v.record.history
                ],
            }
            for v in sorted(versions, key=lambda v: v.record.version)
        ],
    }


def _versions(caller: Caller, service: RunService, name: str) -> list[Version]:
    caller.require("read")
    caller.require_capability(name)
    mine = [
        v
        for v in Registry(service.settings.capabilities_dir).versions(name)
        if v.capability.target.app_family == caller.tenant.app_family
    ]
    if not mine:
        raise ApiError(
            404, "capability_not_found", f"no capability {name!r} for tenant {caller.tenant.id!r}"
        )
    return mine


def _invocable(v: Version) -> bool:
    return lifecycle.invocable(v.status) and requirements.refusal(v.capability) is None


def _summary(
    name: str, version: int, status: str, versions: int, entry: catalog.Entry
) -> dict[str, Any]:
    cap = entry.capability
    return {
        "name": name,
        "version": version,
        "status": status,
        "invocable": entry.invocable,
        "description": cap.description,
        "side_effects": cap.contract.side_effects,
        "versions": versions,
        "links": {"self": f"/capabilities/{name}", "versions": f"/capabilities/{name}/versions"},
    }


def _detail(v: Version, versions: int) -> dict[str, Any]:
    cap = v.capability
    c = cap.contract
    return {
        "name": cap.name,
        "version": cap.version,
        "status": v.status,
        "invocable": _invocable(v),
        "unfit": requirements.refusal(cap),
        "description": cap.description,
        "app_family": cap.target.app_family,
        "surface": cap.target.surface,
        "side_effects": c.side_effects,
        "idempotent": c.idempotent,
        "needs_idempotency_key": c.side_effects != "none" or not c.idempotent,
        "may_escalate": c.may_escalate,
        "outcomes": sorted(c.outcomes),
        "outputs": {n: {"type": o.type, "optional": o.optional} for n, o in cap.outputs.items()},
        "tool": catalog.tool_definition(cap),
        "artifact_hash": v.record.artifact_hash,
        "approved_by": v.record.approved_by,
        "approved_at": v.record.approved_at,
        "versions": versions,
    }
