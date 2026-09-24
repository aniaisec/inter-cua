"""Which run, which request, which tenant and which capability: the ids every
event of a run carries, read once from its ``run.json``.

``run.json`` has two shapes. A replay run names the capability it ran (id,
name, version) and the request (inputs, idempotency key, injected failure).
A discovery run names the goal it was given and the model it asked; the
capability it may become does not exist yet, so only the goal's name is known.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

RunKind = Literal["replay", "discovery"]


class Correlation(BaseModel):
    """What every event of one run carries, so any event found on its own
    (in a log shipper, a dashboard) can be joined back to its run.

    ``invocation_id`` names the caller's request, which may take more than one
    run: a request retried with the same idempotency key is one invocation
    (``idem:<key>``), and a duplicate commit shows up as two commits under one
    invocation. Without a key each run is its own invocation and the id is the
    run's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    invocation_id: str
    tenant_id: str | None = None
    capability: str | None = None
    """The name: stable across versions, and what a person asks about."""
    capability_id: str | None = None
    capability_version: int | None = None


class RunHeader(BaseModel):
    """What was asked, before anything happened."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RunKind
    correlation: Correlation
    started_at: str | None = None
    provider: str | None = None
    model: str | None = None
    """For discovery: the model asked for. The one that answered (an alias
    resolves to a version) is on each model call."""
    inject: str | None = None
    """A failure injected into the mock app for this run (tests, benchmarks).
    Such a run measures the fault, not the capability."""
    goal: str | None = None


class HeaderError(ValueError):
    pass


def read_header(run_dir: Path) -> RunHeader:
    path = run_dir / "run.json"
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HeaderError(f"{path.as_posix()}: {exc}") from exc
    return header_from(raw, fallback_id=run_dir.name)


def header_from(raw: dict[str, Any], *, fallback_id: str) -> RunHeader:
    run_id = str(raw.get("run_id") or fallback_id)
    tenant = raw.get("tenant") or {}
    tenant_id = tenant.get("id") if isinstance(tenant, dict) else None
    if raw.get("kind") == "replay" or "capability" in raw:
        cap = raw.get("capability") or {}
        request = raw.get("request") or {}
        key = request.get("idempotency_key")
        return RunHeader(
            kind="replay",
            correlation=Correlation(
                run_id=run_id,
                invocation_id=f"idem:{key}" if key else run_id,
                tenant_id=tenant_id,
                capability=cap.get("name"),
                capability_id=cap.get("id"),
                capability_version=cap.get("version"),
            ),
            started_at=raw.get("started_at"),
            inject=request.get("inject"),
        )
    goal = raw.get("goal") or {}
    return RunHeader(
        kind="discovery",
        correlation=Correlation(
            run_id=run_id,
            invocation_id=run_id,
            tenant_id=tenant_id,
            capability=goal.get("name") if isinstance(goal, dict) else None,
        ),
        started_at=raw.get("started_at"),
        provider=raw.get("provider"),
        model=raw.get("model"),
        inject=_inject_of(raw.get("entry_url")),
        goal=goal.get("goal") if isinstance(goal, dict) else None,
    )


def _inject_of(url: object) -> str | None:
    """Discovery arms a mock-app failure through the entry URL's query."""
    if not isinstance(url, str) or "inject=" not in url:
        return None
    return url.split("inject=", 1)[1].split("&", 1)[0] or None
