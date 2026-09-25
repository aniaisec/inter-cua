"""A version's health, from the replays of it on record — nothing else.

There is no confidence score and no default: a version with no replay on
record has no health (``None``), not a perfect one. The rates and what they
leave out (runs with an injected fault, runs refused for want of consent)
are ``cua.observability.health``'s; this module only picks the runs of one
capability out of the run directories, cheaply, by their ``run.json``.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from cua.observability.correlation import HeaderError, read_header
from cua.observability.cost import PriceTable
from cua.observability.health import Health, capability_health
from cua.observability.metrics import RunMetrics, run_metrics
from cua.observability.recorder import find_runs, read_run

DEFAULT_ROOTS = (Path("evidence"), Path("bench/runs"))


def replays_of(
    name: str, roots: Iterable[Path] = DEFAULT_ROOTS, prices: PriceTable | None = None
) -> list[RunMetrics]:
    out = []
    for path in find_runs(list(roots)):
        try:
            header = read_header(path)
            if header.kind != "replay" or header.correlation.capability != name:
                continue
            out.append(run_metrics(read_run(path, prices)))
        except (HeaderError, ValueError):
            continue  # `cua metrics report` names unreadable runs; health skips them
    return out


def health_by_version(
    name: str, runs: list[RunMetrics], *, include_injected: bool = False
) -> dict[int, Health]:
    """Version → health, for each version with a replay on record."""
    versions = sorted({r.capability_version for r in runs if r.capability_version is not None})
    out = {}
    for v in versions:
        h = capability_health(runs, name, version=v, include_injected=include_injected)
        if h is not None:
            out[v] = h
    return out


def summary(h: Health) -> dict[str, object]:
    """The rates a registry record carries; the full table is
    ``cua registry health``."""
    return {
        "runs": h.runs,
        "success_rate": h.success_rate,
        "failure_rate": h.failure_rate,
        "escalation_rate": h.escalation_rate,
        "human_intervention_rate": h.human_intervention_rate,
        "drift_rate": h.drift_rate,
        "locator_failure_rate": h.locator_failure_rate,
        "side_effect_unknown_rate": h.side_effect_unknown_rate,
        "p95_latency_s": h.p95_latency_s,
        "last_success": h.last_success.at if h.last_success else None,
        "last_failure": h.last_failure.at if h.last_failure else None,
    }
