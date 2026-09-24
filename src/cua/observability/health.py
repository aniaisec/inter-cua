"""How an approved capability is doing, from every replay of it on record.

Health is computed per capability and per version: a new version is a new
artifact, and its record starts clean rather than inheriting the old one's.

Rates are over the runs found, with the count beside them. A rate over three
runs is an observation, not a measurement; the report says how many.

**Injected runs are left out by default.** A run with a failure injected into
the mock app (a test, a benchmark's server-error task) measures how the
capability handles the fault, which it was asked to face; counting it would
report a healthy capability as failing. ``include_injected`` puts them back.

**So are runs refused for want of consent.** A request to run a step that
commits a change, sent without an approval, is stopped by the policy before
that step (``POLICY_BLOCKED``, needs approval). That is the runtime protecting
the caller; the capability was never allowed to do its work. They are counted
(``refused``) but not rated.

**Outcome definitions.**

``success``      the run completed: outputs delivered, or a declared business
                 outcome (NOT_FOUND) read and reported. The capability worked.
``failure``      the run ended with a failure code.
``escalation``   the run ended needing a person, or handed over to one.
``human``        a person acted during the run (took control, approved,
                 handed back, aborted).
``locator failure``  a control could not be found at all.
``drift``        a control was found, but not as recorded: a lower rung of
                 the ladder, a slip, or the screen changing under a reference.
``side effect unknown``  the run cannot say whether its commit landed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from cua.observability.metrics import RunMetrics


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunRef(_Model):
    run_id: str
    at: str
    outcome: str


class Health(_Model):
    capability: str
    version: int | None
    """None: every version together."""
    runs: int
    injected_excluded: int = 0
    refused: int = 0
    """Runs stopped for want of an approval: counted, not rated."""
    success_rate: float
    failure_rate: float
    escalation_rate: float
    human_intervention_rate: float
    locator_failure_rate: float
    drift_rate: float
    side_effect_unknown_rate: float
    mean_latency_s: float
    p95_latency_s: float
    recoveries_per_run: float
    last_success: RunRef | None = None
    last_failure: RunRef | None = None
    failures: dict[str, int] = Field(default_factory=dict)
    """Outcome label → count, for runs that did not complete."""


def capability_health(
    runs: Iterable[RunMetrics],
    capability: str,
    *,
    version: int | None = None,
    include_injected: bool = False,
) -> Health | None:
    """None when no replay of this capability (and version) is on record."""
    mine = [
        r
        for r in runs
        if r.kind == "replay"
        and r.capability == capability
        and (version is None or r.capability_version == version)
    ]
    injected = [r for r in mine if r.inject]
    if not include_injected:
        mine = [r for r in mine if not r.inject]
    refused = [r for r in mine if r.consent_missing]
    mine = [r for r in mine if not r.consent_missing]
    if not mine:
        return None
    mine.sort(key=lambda r: r.ended_at)
    n = len(mine)

    def rate(count: int) -> float:
        return round(count / n, 4)

    ok = [r for r in mine if r.status == "completed"]
    bad = [r for r in mine if r.status != "completed"]
    failures: dict[str, int] = defaultdict(int)
    for r in bad:
        failures[r.outcome] += 1
    return Health(
        capability=capability,
        version=version,
        runs=n,
        injected_excluded=0 if include_injected else len(injected),
        refused=len(refused),
        success_rate=rate(len(ok)),
        failure_rate=rate(sum(1 for r in mine if r.status == "failed")),
        escalation_rate=rate(sum(1 for r in mine if _escalated(r))),
        human_intervention_rate=rate(sum(1 for r in mine if r.human.intervened)),
        locator_failure_rate=rate(sum(1 for r in mine if r.locator_failures)),
        drift_rate=rate(sum(1 for r in mine if r.locator_drift)),
        side_effect_unknown_rate=rate(sum(1 for r in mine if r.side_effect == "unknown")),
        mean_latency_s=round(sum(r.duration_s for r in mine) / n, 3),
        p95_latency_s=percentile([r.duration_s for r in mine], 95),
        recoveries_per_run=round(sum(r.recoveries for r in mine) / n, 3),
        last_success=_ref(ok[-1]) if ok else None,
        last_failure=_ref(bad[-1]) if bad else None,
        failures=dict(sorted(failures.items(), key=lambda kv: (-kv[1], kv[0]))),
    )


def all_health(
    runs: list[RunMetrics], *, include_injected: bool = False, by_version: bool = True
) -> list[Health]:
    """Every capability with a replay on record: each version, or all versions
    together."""
    keys = sorted(
        {
            (r.capability, r.capability_version if by_version else None)
            for r in runs
            if r.kind == "replay" and r.capability
        },
        key=lambda k: (k[0] or "", k[1] or 0),
    )
    out: list[Health] = []
    for name, version in keys:
        assert name is not None
        h = capability_health(runs, name, version=version, include_injected=include_injected)
        if h is not None:
            out.append(h)
    return out


def _escalated(r: RunMetrics) -> bool:
    return r.status == "escalated" or r.human.handoffs > 0


def _ref(r: RunMetrics) -> RunRef:
    return RunRef(run_id=r.run_id, at=r.ended_at, outcome=r.outcome)


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank: the smallest value with at least ``pct``% at or below it."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * pct // 100))
    return round(ordered[int(rank) - 1], 3)
