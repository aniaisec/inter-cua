"""Which version a request runs, and whether it may start.

**By name** (``cua catalog invoke <name>``): the highest approved version.
A deprecated or revoked version is never picked by default. When no version
is approved, the highest one is returned anyway so that replay turns it away
with a typed ``POLICY_BLOCKED`` result, as it does any unapproved capability,
rather than the caller getting a usage error that looks like a typo.

**By name and version** (``--version 2``): exactly that version, or an error
naming the ones there are. Naming a deprecated version runs it, with a warning:
the caller has pinned it on purpose.

**Starting and in-flight executions.** The lifecycle is checked when an
execution starts, and only then:

* a **revoked** version starts nothing: ``cua replay``, ``cua catalog invoke``
  and ``cua resume`` of a run that was paused for a person all refuse it;
* a run **already executing** when its version is revoked runs to its end.
  Stopping it between steps could stop it between a commit and the
  confirmation that proves the commit, turning a known outcome into
  ``side_effect: unknown`` — worse for the caller than letting it finish;
* a retry that the idempotency cache answers returns the stored result of the
  run that already happened. It starts nothing, and refusing it would tell
  the caller a committed change did not happen.
"""

from __future__ import annotations

from cua.registry import lifecycle
from cua.registry.models import Status, Version
from cua.registry.store import Registry, RegistryError


class Unresolvable(LookupError):
    """No such capability, or no such version of it."""


def resolve(registry: Registry, name: str, version: int | None = None) -> Version:
    mine = registry.versions(name)
    if not mine:
        raise Unresolvable(registry.unknown(name))
    if version is not None:
        try:
            return registry.version(name, version)
        except RegistryError as exc:
            raise Unresolvable(str(exc)) from None
    return default_version(mine)


def default_version(versions: list[Version]) -> Version:
    """The highest approved version, else the highest there is."""
    ranked = sorted(versions, key=lambda v: v.record.version)
    approved = [v for v in ranked if lifecycle.invocable(v.status)]
    return approved[-1] if approved else ranked[-1]


def refusal(name: str, version: int, status: Status) -> str | None:
    """Why a new execution of this version may not start; None if it may (a
    draft is left to replay's own approval gate, which knows about
    ``--allow-draft``)."""
    if status == "revoked":
        return (
            f"{name} v{version} is revoked and starts no new execution; "
            f"see `cua registry show {name} --version {version}`"
        )
    return None


def warning(name: str, version: int, status: Status) -> str | None:
    if status == "deprecated":
        return (
            f"{name} v{version} is deprecated: it runs because it was asked for by path or "
            "version, and it will not be chosen by default"
        )
    return None
