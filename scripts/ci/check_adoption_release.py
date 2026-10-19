"""Check reviewed PR11 evidence; this does not run gates or publish a release."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

GATES = (
    "quality",
    "browser",
    "examples",
    "security",
    "windows",
    "providers",
    "installed",
    "desktop",
    "documentation",
)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Evidence(Record):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Gate(Record):
    status: Literal["pending", "passed", "failed"]
    evidence: Evidence | None = None


class Trial(Record):
    participant: str = Field(min_length=1)
    platform: Literal["windows", "linux"]
    unfamiliar_with_code: bool
    completed: bool
    no_source_edits: bool
    demo_minutes: float = Field(ge=0, allow_inf_nan=False)
    capability_minutes: float = Field(ge=0, allow_inf_nan=False)
    evidence: Evidence


class Release(Record):
    schema_version: Literal[1]
    version: str = Field(min_length=1)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    wheel: Evidence
    gates: dict[str, Gate]
    trials: list[Trial]
    open_blockers: list[str]


def verify_evidence(evidence: Evidence, root: Path) -> None:
    path = (root / evidence.path).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"evidence must stay inside the record directory: {evidence.path}")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != evidence.sha256:
        raise ValueError(f"evidence hash mismatch: {evidence.path}")


def check(path: Path) -> dict[str, object]:
    release = Release.model_validate_json(path.read_text(encoding="utf-8"))
    if set(release.gates) != set(GATES):
        raise ValueError(f"gates must be exactly: {', '.join(GATES)}")
    verify_evidence(release.wheel, path.parent)
    with zipfile.ZipFile(path.parent / release.wheel.path) as archive:
        metadata_paths = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_paths) != 1:
            raise ValueError("wheel must contain exactly one package metadata record")
        metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
    if metadata["Name"] != "inter-cua" or metadata["Version"] != release.version:
        raise ValueError("wheel name/version does not match release record")
    for name, gate in release.gates.items():
        if gate.status != "passed" or gate.evidence is None:
            raise ValueError(f"gate has no passing reviewed evidence: {name}")
        verify_evidence(gate.evidence, path.parent)
    if release.open_blockers:
        raise ValueError("release has unresolved blockers")
    if len(release.trials) < 2:
        raise ValueError("at least two unfamiliar developers must complete the trial")
    participants = [trial.participant.strip().casefold() for trial in release.trials]
    if any(not name for name in participants) or len(set(participants)) != len(participants):
        raise ValueError("trial participants must have distinct nonempty anonymous IDs")
    goals_missed = []
    for trial in release.trials:
        if not (trial.unfamiliar_with_code and trial.completed and trial.no_source_edits):
            raise ValueError(f"trial is incomplete or ineligible: {trial.participant}")
        verify_evidence(trial.evidence, path.parent)
        if trial.demo_minutes > 15 or trial.capability_minutes > 60:
            goals_missed.append(trial.participant)
    return {
        "status": "ready_for_review",
        "version": release.version,
        "commit": release.commit,
        "trials": len(release.trials),
        "timing_goals_missed": goals_missed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    args = parser.parse_args()
    try:
        result = check(args.record)
    except (OSError, ValueError, ValidationError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"adoption release not ready: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
