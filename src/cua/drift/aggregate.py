"""How often drift happens: ``drift_events / invocations``, by capability
version, by tenant, and by the locator rung the control was recorded on.

**Invocations** are callers' requests (``invocation_id``: a request retried
with one idempotency key is one invocation), counting only those whose run
reached the app. A request refused before a browser started (a draft, bad
inputs, no consent) could not have seen drift, and counting it would dilute
the rate with requests that never looked.

**By rung**, the denominator is lookups rather than invocations: the controls
looked up on a ``role_name`` rung, say, and how many of those lookups
drifted. That is the rate that says which kind of locator ages worst.

Drift the mock app was told to produce (an injected fault) is drift all the
same, and is counted; each row says how much of it was injected, so a real
rate can be read off beside it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable

from pydantic import BaseModel, ConfigDict, Field

from cua.drift.models import DriftEvent, RunDrift


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Rate(_Model):
    key: str
    drift_events: int
    denominator: int
    """Invocations, or for a rung, lookups on that rung."""
    rate: float | None
    """None when there is nothing to divide by."""
    fatal: int
    """Events that stopped their run."""
    injected: int
    """Events from runs with a fault injected into the app on purpose."""
    kinds: dict[str, int] = Field(default_factory=dict)


class DriftReport(_Model):
    runs: int
    invocations: int
    drift_events: int
    rate: float | None
    by_capability: list[Rate]
    by_tenant: list[Rate]
    by_rung: list[Rate]
    by_kind: dict[str, int]


def aggregate(runs: Iterable[RunDrift], *, exclude_injected: bool = False) -> DriftReport:
    runs = [r for r in runs if r.reached_app and not (exclude_injected and r.injected)]
    events = [e for r in runs for e in r.events]

    def invocations(pick: Callable[[RunDrift], str]) -> dict[str, int]:
        seen: dict[str, set[str]] = defaultdict(set)
        for r in runs:
            seen[pick(r)].add(r.invocation_id)
        return {k: len(v) for k, v in seen.items()}

    lookups: Counter[str] = Counter()
    for r in runs:
        lookups.update(r.lookups)

    total = len({r.invocation_id for r in runs})
    return DriftReport(
        runs=len(runs),
        invocations=total,
        drift_events=len(events),
        rate=_rate(len(events), total),
        by_capability=_rows(
            events,
            invocations(lambda r: f"{r.capability} v{r.version}"),
            lambda e: f"{e.capability} v{e.version}",
        ),
        by_tenant=_rows(events, invocations(lambda r: r.tenant_id), lambda e: e.tenant_id),
        by_rung=_rows(
            [e for e in events if e.expected_locator_rung != "-"],
            dict(lookups),
            lambda e: e.expected_locator_rung,
        ),
        by_kind=dict(Counter(e.kind for e in events).most_common()),
    )


def _rows(
    events: list[DriftEvent], denominators: dict[str, int], key: Callable[[DriftEvent], str]
) -> list[Rate]:
    grouped: dict[str, list[DriftEvent]] = defaultdict(list)
    for e in events:
        grouped[key(e)].append(e)
    out = []
    for k in sorted(set(denominators) | set(grouped)):
        mine = grouped.get(k, [])
        n = denominators.get(k, 0)
        out.append(
            Rate(
                key=k,
                drift_events=len(mine),
                denominator=n,
                rate=_rate(len(mine), n),
                fatal=sum(1 for e in mine if e.fatal),
                injected=sum(1 for e in mine if e.injected),
                kinds=dict(Counter(e.kind for e in mine).most_common()),
            )
        )
    return out


def _rate(count: int, n: int) -> float | None:
    return round(count / n, 4) if n else None
