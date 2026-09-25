"""The registry on disk, beside the capabilities it describes::

    capabilities/
        member_savings_balance.json          the working copy: what `cua record`
                                             writes and `cua describe` reads
        registry/
            lifecycle.jsonl                  every transition after review
            member_savings_balance/v3.json   each approved version, sealed

The working copies stay where they were, so every existing command and path
keeps working. What the registry adds is that an approved version outlives
its working copy: re-recording a capability replaces
``member_savings_balance.json`` with a new draft, and the approved version it
replaced is still under ``registry/`` and still runs. That is how several
versions coexist.

A registered file is never changed. Registering the same version again with
different content is refused: it would mean an approval, or a revocation,
silently came to cover something else.

Everything here is files in the repository — no service, no network — so the
registry is read, tested and reviewed offline, and its history is the git
history of these files.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from cua.artifact.schema import Capability
from cua.artifact.store import CAPABILITIES_DIR, ArtifactError, Loaded, open_capability
from cua.policy.approval import STATE_DIR, last_review
from cua.registry import lifecycle
from cua.registry.models import LedgerEntry, Record, Status, Version
from cua.tenant import TENANTS_DIR, load_tenant

REGISTRY = "registry"
LEDGER = "lifecycle.jsonl"


class RegistryError(ValueError):
    """The registry cannot do what was asked, and why."""


@dataclass(frozen=True)
class Unreadable:
    path: Path
    error: str


class Registry:
    def __init__(
        self,
        capabilities_dir: Path = CAPABILITIES_DIR,
        *,
        state_dir: Path = STATE_DIR,
        tenants_dir: Path = TENANTS_DIR,
    ) -> None:
        self.capabilities_dir = capabilities_dir
        self.root = capabilities_dir / REGISTRY
        self.ledger_path = self.root / LEDGER
        self.state_dir = state_dir
        self.tenants_dir = tenants_dir
        self.unreadable: list[Unreadable] = []
        """Files skipped by the last scan, with the reason."""

    @classmethod
    def for_path(cls, path: Path, *, state_dir: Path = STATE_DIR) -> Registry:
        """The registry a capability file belongs to: beside a working copy
        (``capabilities/x.json``), or the one a registered copy is inside
        (``capabilities/registry/x/v3.json``), or the one a candidate repair
        is proposed to (``capabilities/candidates/x/v4/capability.json``)."""
        from cua.drift.store import capabilities_dir_of, is_candidate_path

        parent = path.parent
        if is_candidate_path(path):
            return cls(capabilities_dir_of(path), state_dir=state_dir)
        if parent.parent.name == REGISTRY:
            return cls(parent.parent.parent, state_dir=state_dir)
        return cls(parent, state_dir=state_dir)

    # -- the ledger ----------------------------------------------------------

    def ledger(self) -> list[LedgerEntry]:
        if not self.ledger_path.is_file():
            return []
        entries: list[LedgerEntry] = []
        for n, line in enumerate(self.ledger_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                entries.append(LedgerEntry.model_validate_json(line))
            except ValidationError as exc:
                # A ledger that cannot be read cannot be trusted to hold no
                # revocation: refuse to answer rather than answer "approved".
                raise RegistryError(
                    f"{self.ledger_path.as_posix()} line {n} is not a ledger entry: "
                    f"{exc.errors()[0]['msg']}"
                ) from None
        return entries

    def _append(self, entry: LedgerEntry) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(entry.model_dump_json() + "\n")

    def status_of(self, capability: Capability) -> Status:
        """Where this capability, as loaded, stands. What ``cua replay`` asks
        before it starts a run."""
        entries = [
            e
            for e in self.ledger()
            if e.name == capability.name and e.version == capability.version
        ]
        return lifecycle.fold(self._base(capability), entries)

    def _base(self, capability: Capability) -> Status:
        review = None
        if capability.approval_state != "approved":
            try:
                review = last_review(capability, state_dir=self.state_dir)
            except (OSError, ValueError):
                review = None
        reviewed = (
            review is not None
            and review.version == capability.version
            and review.content_sha256 == capability.content_hash()
        )
        return lifecycle.base_status(capability.approval_state, reviewed=reviewed)

    # -- versions ------------------------------------------------------------

    def snapshot_path(self, name: str, version: int) -> Path:
        return self.root / name / f"v{version}.json"

    def _working_copies(self) -> Iterator[Loaded]:
        if not self.capabilities_dir.is_dir():
            return
        for path in sorted(self.capabilities_dir.glob("*.json")):
            try:
                yield open_capability(path)
            except ArtifactError as exc:
                self.unreadable.append(Unreadable(path, str(exc).splitlines()[0]))

    def _snapshots(self) -> Iterator[Loaded]:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.glob("*/v*.json")):
            try:
                loaded = open_capability(path)
            except ArtifactError as exc:
                self.unreadable.append(Unreadable(path, str(exc).splitlines()[0]))
                continue
            cap = loaded.capability
            if loaded.edited_outside or path != self.snapshot_path(cap.name, cap.version):
                # A registered file is never changed; one that has been is not
                # the version that was approved, whatever it now says.
                self.unreadable.append(
                    Unreadable(path, "changed after it was registered; restore it from git")
                )
                continue
            yield loaded

    def all_versions(self) -> list[Version]:
        """Every version on disk, registered or working copy, by name and
        number."""
        self.unreadable = []
        ledger = self.ledger()
        scope = self._tenant_scope()
        found: dict[tuple[str, int], tuple[Loaded, bool, bool]] = {}
        for loaded in self._snapshots():
            cap = loaded.capability
            found[(cap.name, cap.version)] = (loaded, True, False)
        for loaded in self._working_copies():
            cap = loaded.capability
            key = (cap.name, cap.version)
            if key in found:
                kept = found[key][0].capability
                if kept.content_hash() != cap.content_hash():
                    self.unreadable.append(
                        Unreadable(
                            loaded.path,
                            f"says it is {cap.name} v{cap.version}, but differs from the "
                            f"registered v{cap.version}",
                        )
                    )
                    continue
                found[key] = (found[key][0], True, True)
            else:
                found[key] = (loaded, False, True)
        out = []
        for (name, version), (loaded, registered, working) in sorted(found.items()):
            entries = [e for e in ledger if e.name == name and e.version == version]
            cap = loaded.capability
            status = lifecycle.fold(self._base(cap), entries)
            record = Record(
                name=name,
                version=version,
                status=status,
                capability_id=cap.id,
                description=cap.description,
                app_family=cap.target.app_family,
                surface=cap.target.surface,
                tenant_scope=scope.get(cap.target.app_family, []),
                side_effects=cap.contract.side_effects,
                artifact_hash=cap.content_hash(),
                created_at=cap.provenance.recorded_at,
                approved_at=cap.approved_at,
                approved_by=cap.approved_by,
                registered=registered,
                working_copy=working,
                edited_outside=loaded.edited_outside,
                path=loaded.path.as_posix(),
                history=entries,
            )
            out.append(Version(record, cap, loaded.path))
        return out

    def versions(self, name: str) -> list[Version]:
        return [v for v in self.all_versions() if v.record.name == name]

    def names(self) -> list[str]:
        return sorted({v.record.name for v in self.all_versions()})

    def _tenant_scope(self) -> dict[str, list[str]]:
        """App family → the tenants that run it."""
        out: dict[str, list[str]] = {}
        if not self.tenants_dir.is_dir():
            return out
        for path in sorted(self.tenants_dir.glob("*.yaml")):
            try:
                tenant = load_tenant(str(path))
            except (OSError, ValueError):
                continue
            out.setdefault(tenant.app_family, []).append(tenant.id)
        return out

    # -- changes -------------------------------------------------------------

    def register(self, capability: Capability, *, note: str = "") -> Path:
        """Keep a sealed copy of an approved version and record its approval.
        Doing it again for the same content changes nothing."""
        if capability.approval_state != "approved" or not capability.approved_by:
            raise RegistryError(
                f"{capability.name} v{capability.version} is not approved; only an approved "
                "version is registered"
            )
        path = self.snapshot_path(capability.name, capability.version)
        if path.is_file():
            try:
                existing = open_capability(path).capability
            except ArtifactError as exc:
                raise RegistryError(str(exc)) from None
            if existing.content_hash() != capability.content_hash():
                raise RegistryError(
                    f"{path.as_posix()} already holds a different {capability.name} "
                    f"v{capability.version}; a registered version is never replaced"
                )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            sealed = capability
            if sealed.content_sha256 != sealed.content_hash():
                raise RegistryError(
                    f"{capability.name} v{capability.version} is not sealed; approve it "
                    "with `cua approve` so it is saved first"
                )
            path.write_text(sealed.to_json(), encoding="utf-8", newline="\n")
        if not any(
            e.name == capability.name and e.version == capability.version for e in self.ledger()
        ):
            self._append(
                LedgerEntry(
                    name=capability.name,
                    version=capability.version,
                    capability_id=capability.id,
                    artifact_hash=capability.content_hash(),
                    previous="review",
                    status="approved",
                    by=capability.approved_by,
                    at=capability.approved_at or _now(),
                    reason=note,
                )
            )
        return path

    def sync(self) -> list[Path]:
        """Register every approved working copy that is not registered yet:
        capabilities approved before the registry existed."""
        written = []
        for v in self.all_versions():
            if v.record.working_copy and not v.record.registered and v.status == "approved":
                written.append(self.register(v.capability, note="registered by cua registry sync"))
        return written

    def transition(
        self, name: str, version: int, change: lifecycle.Change, *, by: str, reason: str = ""
    ) -> Record:
        by = by.strip()
        if not by:
            raise RegistryError("say who is making the change (--by <name>)")
        if change == "revoke" and not reason.strip():
            raise RegistryError("say why it is revoked (--reason)")
        target = self.version(name, version)
        to = lifecycle.TARGET[change]
        try:
            lifecycle.check(target.status, to)
        except lifecycle.LifecycleError as exc:
            raise RegistryError(f"cannot {change} {name} v{version}: {exc}") from None
        if not target.record.registered:
            self.register(target.capability)  # the decision is about a version kept
        self._append(
            LedgerEntry(
                name=name,
                version=version,
                capability_id=target.capability.id,
                artifact_hash=target.record.artifact_hash,
                previous=target.status,
                status=to,
                by=by,
                at=_now(),
                reason=reason.strip(),
            )
        )
        return self.version(name, version).record

    def version(self, name: str, version: int) -> Version:
        mine = self.versions(name)
        for v in mine:
            if v.record.version == version:
                return v
        if not mine:
            raise RegistryError(self.unknown(name))
        known = ", ".join(f"v{v.record.version}" for v in mine)
        raise RegistryError(f"{name} has no v{version} (known: {known})")

    def unknown(self, name: str) -> str:
        known = ", ".join(self.names()) or "none"
        return (
            f"no capability named {name!r} in {self.capabilities_dir.as_posix()} (known: {known})"
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
