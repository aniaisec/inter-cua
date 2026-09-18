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

    loaded = open_capability(path)
    capability = loaded.capability
    label = f"{capability.name} v{capability.version}"
    if capability.approval_state == "approved":
        return capability

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
    return save(approved, path)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
