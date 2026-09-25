"""Candidate repairs on disk, beside the registry, never inside it::

    capabilities/
        member_savings_balance.json           the working copy (untouched)
        registry/                             approved versions (untouched)
        candidates/
            member_savings_balance/
                v4/
                    capability.json           the proposed version, a draft
                    candidate.json            what it repairs, the change, the
                                              evidence, checks and evaluation
                    rationale.md              the same, for the reviewer
                    evidence/                 the failure screen it was built from

A candidate is not a version anyone can call. The registry does not read
this directory, ``cua catalog`` does not offer it, and ``cua replay`` runs it
only with ``--allow-draft``. It becomes a version the way any capability
does: a person describes it and approves it (``cua describe``, ``cua
approve``), and approval is refused until its evaluation has passed on
exactly this content (``approval_refusal``). Rejecting it records who and
why, and leaves it where it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from cua.artifact.schema import Capability
from cua.artifact.store import CAPABILITIES_DIR, ArtifactError, open_capability
from cua.drift.models import CandidateRecord

CANDIDATES = "candidates"
CAPABILITY = "capability.json"
RECORD = "candidate.json"
RATIONALE = "rationale.md"
EVIDENCE = "evidence"

_VERSION_DIR = re.compile(r"^v([1-9][0-9]*)$")


class CandidateError(ValueError):
    """No candidate can be made, found or changed as asked, and why."""


@dataclass(frozen=True)
class Candidate:
    dir: Path
    record: CandidateRecord
    capability: Capability
    edited_outside: bool = False
    """``capability.json`` was changed by hand after it was proposed: it is
    not the content the record, its checks or its evaluation are about."""

    @property
    def path(self) -> Path:
        return self.dir / CAPABILITY

    @property
    def label(self) -> str:
        return f"{self.record.name} v{self.record.version}"


class Candidates:
    def __init__(self, capabilities_dir: Path = CAPABILITIES_DIR) -> None:
        self.capabilities_dir = capabilities_dir
        self.root = capabilities_dir / CANDIDATES

    def dir_for(self, name: str, version: int) -> Path:
        return self.root / name / f"v{version}"

    def all(self, name: str | None = None) -> list[Candidate]:
        if not self.root.is_dir():
            return []
        out = []
        for d in sorted(self.root.glob(f"{name or '*'}/v*")):
            if d.is_dir() and _VERSION_DIR.match(d.name) and (d / RECORD).is_file():
                out.append(self.read(d))
        return sorted(out, key=lambda c: (c.record.name, c.record.version))

    def versions(self, name: str) -> list[int]:
        if not (self.root / name).is_dir():
            return []
        return sorted(
            int(m.group(1))
            for d in (self.root / name).iterdir()
            if (m := _VERSION_DIR.match(d.name)) is not None
        )

    def get(self, name: str, version: int | None = None) -> Candidate:
        mine = self.all(name)
        if not mine:
            raise CandidateError(f"no candidate for {name!r} in {self.root.as_posix()}")
        if version is None:
            return mine[-1]
        for c in mine:
            if c.record.version == version:
                return c
        known = ", ".join(f"v{c.record.version}" for c in mine)
        raise CandidateError(f"{name} has no candidate v{version} (candidates: {known})")

    def read(self, d: Path) -> Candidate:
        try:
            record = CandidateRecord.model_validate_json((d / RECORD).read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            raise CandidateError(f"{(d / RECORD).as_posix()} cannot be read: {exc}") from None
        try:
            loaded = open_capability(d / CAPABILITY)
        except ArtifactError as exc:
            raise CandidateError(str(exc)) from None
        edited = loaded.edited_outside or loaded.capability.content_hash() != record.artifact_hash
        return Candidate(d, record, loaded.capability, edited_outside=edited)

    def write(self, d: Path, record: CandidateRecord) -> None:
        from cua.drift.rationale import render

        d.mkdir(parents=True, exist_ok=True)
        (d / RECORD).write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
        (d / RATIONALE).write_text(render(record, d.as_posix()), encoding="utf-8", newline="\n")


def is_candidate_path(path: Path) -> bool:
    """``capabilities/candidates/<name>/v<N>/capability.json``."""
    return (
        path.name == CAPABILITY
        and _VERSION_DIR.match(path.parent.name) is not None
        and path.parent.parent.parent.name == CANDIDATES
    )


def capabilities_dir_of(path: Path) -> Path:
    return path.parent.parent.parent.parent


def status(candidate: Candidate, registered: dict[tuple[str, int], tuple[str, str]]) -> str:
    """``registered``: (name, version) → (status, artifact hash) from the
    registry. A candidate approved under its own number is that version now,
    and has that version's status."""
    r = candidate.record
    if candidate.edited_outside:
        return "edited by hand"
    known = registered.get((r.name, r.version))
    if known is not None and known[1] == r.artifact_hash and known[0] not in ("draft", "review"):
        return known[0]
    if r.rejection is not None:
        return "rejected"
    ev = r.evaluation
    if ev is None or ev.artifact_hash != r.artifact_hash:
        return "proposed"
    return "ready for review" if ev.passed else "failed evaluation"


def approval_refusal(path: Path, capability: Capability) -> str | None:
    """Why ``cua approve`` must not approve this candidate; None if it may.
    A repair is approved only after its evaluation passed on this content,
    and never after it was rejected."""
    try:
        candidate = Candidates(capabilities_dir_of(path)).read(path.parent)
    except CandidateError as exc:
        return str(exc)
    r = candidate.record
    evaluate = f"cua drift evaluate {r.name} --version {r.version}"
    if candidate.edited_outside or capability.content_hash() != r.artifact_hash:
        return (
            f"{path.as_posix()} was changed after it was proposed; its checks and evaluation "
            "are about other content. Propose the repair again."
        )
    if r.rejection is not None:
        return f"{candidate.label} was rejected by {r.rejection.by}: {r.rejection.reason}"
    if r.evaluation is None or r.evaluation.artifact_hash != r.artifact_hash:
        return f"{candidate.label} has not been evaluated. Run it first: {evaluate}"
    if not r.evaluation.passed:
        failed = "; ".join(g.detail for g in r.evaluation.gates if not g.passed)
        return f"{candidate.label} failed its evaluation: {failed}"
    return None
