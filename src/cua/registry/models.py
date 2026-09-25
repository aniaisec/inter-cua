"""What the registry records about a capability version.

A version is identified by ``name`` and ``version``: the name is what a caller
asks for and stays the same across versions, and the number is bumped by any
change to what the capability does (``cua.artifact.store``). ``app_family``
says which product it drives, and ``tenant_scope`` which tenants run that
product and so can invoke it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.schema import Capability

Status = Literal["draft", "review", "approved", "deprecated", "revoked"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LedgerEntry(_Model):
    """One line of ``lifecycle.jsonl``: a version moved from one status to
    another, by whom, when, and why. Appended, never rewritten."""

    name: str
    version: int
    capability_id: str
    artifact_hash: str
    """The content the decision was made about."""
    previous: Status
    status: Status
    by: str
    at: str
    reason: str = ""


class Record(_Model):
    """A version as ``cua registry show`` prints it."""

    name: str
    version: int
    status: Status
    capability_id: str
    description: str
    app_family: str
    surface: str
    tenant_scope: list[str]
    """Tenants whose deployment runs ``app_family`` (``tenants/*.yaml``)."""
    side_effects: str
    artifact_hash: str
    created_at: str
    """When the discovery run it was recorded from happened."""
    approved_at: str | None = None
    approved_by: str | None = None
    registered: bool
    """A sealed copy is kept under ``capabilities/registry/``. An approved
    version is registered when it is approved; one that is only the working
    copy is replaced the next time the capability is recorded."""
    working_copy: bool
    """It is what ``capabilities/<name>.json`` holds now."""
    edited_outside: bool = False
    path: str
    history: list[LedgerEntry] = Field(default_factory=list)
    health: dict[str, Any] | None = None
    """From observed replays of this version (``cua.observability.health``),
    when asked for; None when not computed or when nothing is on record."""


@dataclass(frozen=True)
class Version:
    """A version with the artifact itself, for code that runs it."""

    record: Record
    capability: Capability
    path: Path

    @property
    def status(self) -> Status:
        return self.record.status
