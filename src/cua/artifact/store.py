"""Reading and writing capability files, and the rule that keeps approval honest.

**Any change to what a capability does bumps its version and resets it to
draft.** "What it does" is everything except the version and the approval
bookkeeping (``Capability.content_hash``). Two routes to a change are covered:

* through ``save`` — a re-recorded run, a programmatic edit: the new content is
  compared with what is on disk, and a difference is a new, unapproved version;
* around it — someone edits the JSON by hand. Every save writes a seal
  (``content_sha256``); a file whose content no longer matches its seal is
  loaded as the *next* version, in draft, and says so. An approved capability
  therefore cannot be quietly altered and still replay unattended.

The seal detects unreviewed edits. It is not a signature: someone determined
to forge an approval can recompute it, and a deployment that needs to stop
that signs approvals with a key the editor does not hold.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from cua.artifact.schema import Capability

CAPABILITIES_DIR = Path("capabilities")


class ArtifactError(ValueError):
    """A capability file that cannot be used, with every reason why."""


@dataclass(frozen=True)
class Loaded:
    capability: Capability
    path: Path
    edited_outside: bool = False
    """The file's content did not match its seal: it was changed without
    going through ``save``, and ``capability`` is the resulting new draft."""
    sealed_version: int | None = None
    """The version the file said it was, when that differs from what it is."""


def open_capability(path: Path) -> Loaded:
    """Load and validate a capability, treating an unsealed edit as a new draft."""
    capability = parse(_read(path), source=path)
    digest = capability.content_hash()
    if capability.content_sha256 == digest:
        return Loaded(capability, path)

    if capability.content_sha256 is None and capability.approval_state == "draft":
        # Never saved by cua (written by hand from scratch): nothing to reset.
        return Loaded(capability, path)

    bumped = with_changes(
        capability,
        version=capability.version + 1,
        approval_state="draft",
        approved_by=None,
        approved_at=None,
        content_sha256=None,
    )
    return Loaded(bumped, path, edited_outside=True, sealed_version=capability.version)


def load(path: Path) -> Capability:
    return open_capability(path).capability


def save(capability: Capability, path: Path) -> Capability:
    """Write a capability, numbering it against what the file held before.

    Same content as on disk: the caller's version and approval stand (this is
    how an approval is saved). Different content: one past the previous
    version, in draft. The previous file's id is kept — re-recording a
    capability makes a new version of it, not a second capability.
    """
    previous: Capability | None = None
    if path.is_file():
        try:
            previous = load(path)
        except ArtifactError:
            previous = None  # an unreadable file is replaced, not versioned against

    if previous is not None:
        capability = with_changes(capability, id=previous.id)
        if capability.content_hash() != previous.content_hash():
            capability = with_changes(
                capability,
                version=previous.version + 1,
                approval_state="draft",
                approved_by=None,
                approved_at=None,
            )

    sealed = with_changes(capability, content_sha256=capability.content_hash())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sealed.to_json(), encoding="utf-8", newline="\n")
    return sealed


def parse(data: Any, *, source: Path | str = "<capability>") -> Capability:
    try:
        return Capability.model_validate(data)
    except ValidationError as exc:
        raise ArtifactError(format_errors(exc, source)) from None


def with_changes(capability: Capability, **changes: Any) -> Capability:
    """A copy with fields changed, validated again — ``model_copy`` would skip
    the checks that make an artifact safe to replay."""
    data = capability.model_dump(mode="json", by_alias=True)
    data.update(changes)
    return Capability.model_validate(data)


def format_errors(exc: ValidationError, source: Path | str) -> str:
    where = source.as_posix() if isinstance(source, Path) else source
    lines = [f"{where} is not a valid capability:"]
    for error in exc.errors():
        loc = ".".join(str(p) for p in error["loc"])
        message = error["msg"].removeprefix("Value error, ")
        # Several problems found together arrive as one message.
        for part in message.splitlines():
            lines.append(f"  - {loc + ': ' if loc else ''}{part}")
    return "\n".join(lines)


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ArtifactError(f"{path.as_posix()}: no such file") from None
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"{path.as_posix()} is not JSON: {exc}") from None
