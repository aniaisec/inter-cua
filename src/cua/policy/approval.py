"""The approval gate: a capability runs unattended only after a person has
read what it does and said yes to exactly that.

"Read" is made concrete: ``cua describe`` writes a review receipt recording
the version and content hash it rendered. ``cua approve`` refuses unless a
receipt exists for the capability *as it is now* — so approving something
edited since it was last described, or never described at all, is refused
rather than trusted.

Receipts live in a local state directory (``.cua/reviews/``), not in the
artifact: they record what one reviewer saw on one machine, and the approval
they lead to is what gets committed.

An approved version is registered (``cua.registry``): a sealed copy is kept
under ``capabilities/registry/`` and the approval is appended to its ledger,
so the version still runs after the capability is re-recorded. A version the
registry has deprecated or revoked is not approved again here: reinstating a
deprecated one is ``cua registry reinstate``, and a revoked one is final.

A candidate repair (``capabilities/candidates/<name>/v<N>/capability.json``,
``cua drift propose``) is approved the same way, with one more condition: its
evaluation (``cua drift evaluate``) must have passed on exactly this content,
and it must not have been rejected. Its approval is noted in the ledger as a
repair of the version it came from.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from cua.artifact.schema import Capability
from cua.artifact.store import open_capability, save, with_changes

STATE_DIR = Path(".cua")


class ApprovalRefused(Exception):
    """Approval was not given, and why."""


class Review(BaseModel):
    model_config = ConfigDict(frozen=True)

    capability_id: str
    name: str
    version: int
    content_sha256: str
    described_at: str
    path: str


def _receipt(capability_id: str, state_dir: Path) -> Path:
    return state_dir / "reviews" / f"{capability_id}.json"


def record_review(capability: Capability, path: Path, *, state_dir: Path = STATE_DIR) -> Review:
    """Note that this exact content was described to someone."""
    review = Review(
        capability_id=capability.id,
        name=capability.name,
        version=capability.version,
        content_sha256=capability.content_hash(),
        described_at=_now(),
        path=path.as_posix(),
    )
    receipt = _receipt(capability.id, state_dir)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(review.model_dump_json(indent=2), encoding="utf-8")
    return review


def last_review(capability: Capability, *, state_dir: Path = STATE_DIR) -> Review | None:
    receipt = _receipt(capability.id, state_dir)
    if not receipt.is_file():
        return None
    return Review.model_validate(json.loads(receipt.read_text(encoding="utf-8")))


def approve(path: Path, by: str, *, state_dir: Path = STATE_DIR) -> Capability:
    """draft → approved, if the capability is exactly what was last described."""
    by = by.strip()
    if not by:
        raise ApprovalRefused("say who is approving (--by <name>)")

    from cua.drift.store import (
        Candidates,
        approval_refusal,
        capabilities_dir_of,
        is_candidate_path,
    )
    from cua.registry.store import Registry, RegistryError  # it reads receipts from here

    loaded = open_capability(path)
    capability = loaded.capability
    label = f"{capability.name} v{capability.version}"
    registry = Registry.for_path(path, state_dir=state_dir)
    try:
        status = registry.status_of(capability)
        if status == "deprecated":
            raise ApprovalRefused(
                f"{label} is deprecated; to offer it again: cua registry reinstate "
                f"{capability.name} --version {capability.version} --by <your name>"
            )
        if status == "revoked":
            raise ApprovalRefused(
                f"{label} is revoked, which is final; record and approve a new version"
            )
        if capability.approval_state == "approved":
            registry.register(capability)  # approved before the registry existed
            return capability
    except RegistryError as exc:
        raise ApprovalRefused(str(exc)) from None

    note = ""
    if is_candidate_path(path):
        refusal = approval_refusal(path, capability)
        if refusal is not None:
            raise ApprovalRefused(refusal)
        base = Candidates(capabilities_dir_of(path)).read(path.parent).record.base
        note = f"candidate repair of v{base.version} ({path.parent.as_posix()})"

    review = last_review(capability, state_dir=state_dir)
    if review is None:
        raise ApprovalRefused(
            f"{label} has not been described yet. Read it first: cua describe {path.as_posix()}"
        )
    if review.version != capability.version or review.content_sha256 != capability.content_hash():
        edited = " (the file was edited by hand)" if loaded.edited_outside else ""
        raise ApprovalRefused(
            f"{label} has changed since it was last described (described as "
            f"v{review.version}){edited}. Read it again: cua describe {path.as_posix()}"
        )

    approved = with_changes(
        capability, approval_state="approved", approved_by=by, approved_at=_now()
    )
    saved = save(approved, path)
    try:
        registry.register(saved, note=note)
    except RegistryError as exc:
        raise ApprovalRefused(f"approved in {path.as_posix()}, but not registered: {exc}") from None
    return saved


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
