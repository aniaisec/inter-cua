"""Where a capability version stands, and how it may move.

::

    draft ──describe──▶ review ──approve──▶ approved ◀──reinstate──┐
                                              │    │                │
                                         revoke  deprecate ──▶ deprecated
                                              ▼                     │
                                           revoked ◀─────revoke─────┘

``draft``       recorded, not yet read by a person.
``review``      described (``cua describe``) exactly as it is now, on this
                machine; ``cua approve`` will accept it.
``approved``    runs unattended. The one ``cua catalog`` offers by default is
                the highest approved version.
``deprecated``  still runs when a caller names the version, with a warning;
                never chosen by default. ``reinstate`` returns it to approved.
``revoked``     starts no new execution, ever. Terminal: a revoked version's
                content can only run again as a new version, reviewed and
                approved like any other.

``draft → review → approved`` is the approval gate that already exists
(``cua describe`` then ``cua approve``); the registry records the approval and
owns the transitions after it. ``deprecated → revoked`` is allowed: finding a
fault in a version that has already been superseded must not require
reinstating it first, which would make it the default for a moment.

**The status is the artifact's own approval, with the ledger applied on top.**
``approval_state`` is sealed into the capability file by ``cua approve``;
deprecation and revocation are about a version's standing, not its content,
so they are appended to ``lifecycle.jsonl`` rather than written into a file
whose content hash approval depends on. The ledger fails closed: a revocation
applies to that name and version whatever file later claims to be it, and a
ledger entry can never make a version approved that its artifact does not say
is approved.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from cua.registry.models import LedgerEntry, Status

Change = Literal["deprecate", "revoke", "reinstate"]

TRANSITIONS: frozenset[tuple[Status, Status]] = frozenset(
    {
        ("draft", "review"),
        ("review", "approved"),
        ("approved", "deprecated"),
        ("approved", "revoked"),
        ("deprecated", "approved"),
        ("deprecated", "revoked"),
    }
)

TARGET: dict[Change, Status] = {
    "deprecate": "deprecated",
    "revoke": "revoked",
    "reinstate": "approved",
}


class LifecycleError(ValueError):
    """A transition the lifecycle does not allow, and why."""


def check(current: Status, to: Status) -> None:
    if (current, to) in TRANSITIONS:
        return
    if current == to:
        raise LifecycleError(f"it is already {current}")
    if current == "revoked":
        raise LifecycleError(
            "it is revoked, which is final: record and approve a new version instead"
        )
    if to == "approved":
        raise LifecycleError(
            f"it is {current}; a {current} version is approved with `cua describe` and "
            "`cua approve`, not through the registry"
        )
    allowed = sorted(t for f, t in TRANSITIONS if f == current)
    raise LifecycleError(
        f"it is {current}, and a {current} version can only become "
        + (" or ".join(allowed) if allowed else "nothing else")
    )


def base_status(approval_state: str, *, reviewed: bool) -> Status:
    """What the artifact alone says."""
    if approval_state == "approved":
        return "approved"
    return "review" if reviewed else "draft"


def fold(base: Status, entries: Iterable[LedgerEntry]) -> Status:
    """The artifact's status with the ledger's entries for it applied, oldest
    first."""
    status: Status = base
    for entry in entries:
        status = entry.status
    if status == "approved" and base != "approved":
        return base  # the ledger cannot approve what the artifact does not
    return status


def invocable(status: Status) -> bool:
    """Runs unattended, and may be picked by default."""
    return status == "approved"


def may_start(status: Status) -> bool:
    """A new execution may start when the caller names this version."""
    return status in ("approved", "deprecated")
