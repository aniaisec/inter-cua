"""From raw rows to the numbers a comparison needs. Recomputed every time.

Rates are proportions of runs, with a Wilson 95 % interval beside each success
rate: ten runs that all succeed say "somewhere above 72 %", not "100 %".
Latency percentiles are nearest-rank. Costs are summed from each row's own
estimate; a row with no price makes the total unpriced rather than smaller.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from cua.benchmark.models import RunMetrics

REPETITION_MARKS = (1, 10, 50, 100)


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def _rate(count: int, n: int) -> float | None:
    return round(count / n, 4) if n else None


def _cost(rows: list[RunMetrics]) -> Decimal | None:
    if any(r.estimated_cost_usd is None for r in rows):
        return None
    return sum((r.estimated_cost_usd or Decimal(0) for r in rows), Decimal(0))


def stats(rows: list[RunMetrics]) -> dict[str, Any]:
    """Every metric for one group of runs."""
    n = len(rows)
    exact = sum(r.match == "exact" for r in rows)
    durations = [r.duration_s for r in rows]
    cost = _cost(rows)
    by_rep = sorted(rows, key=lambda r: r.repetition)
    tokens_total = sum(r.total_tokens for r in rows)
    business = [r for r in rows if r.truth.startswith("business_outcome")]
    return {
        "runs": n,
        "exact": exact,
        "safe_stop": sum(r.match == "safe_stop" for r in rows),
        "wrong": sum(r.match == "wrong" for r in rows),
        "success_rate": _rate(exact, n),
        "success_ci95": wilson(exact, n),
        "safe_stop_rate": _rate(sum(r.match == "safe_stop" for r in rows), n),
        "wrong_rate": _rate(sum(r.match == "wrong" for r in rows), n),
        "business_outcome_accuracy": _rate(
            sum(r.match == "exact" for r in business), len(business)
        ),
        "escalation_rate": _rate(sum(r.escalated for r in rows), n),
        "human_intervention_rate": _rate(sum(r.human_intervention for r in rows), n),
        "success_rate_after": {
            str(k): _rate(sum(r.match == "exact" for r in by_rep[:k]), k)
            for k in REPETITION_MARKS
            if len(by_rep) >= k
        },
        "latency_mean_s": round(statistics.fmean(durations), 3) if durations else None,
        "latency_median_s": round(statistics.median(durations), 3) if durations else None,
        "latency_p95_s": percentile(durations, 95),
        "latency_stdev_s": round(statistics.stdev(durations), 3) if n > 1 else None,
        "llm_wait_s": round(sum(r.llm_wait_s for r in rows), 3),
        "llm_calls": sum(r.llm_calls for r in rows),
        "llm_calls_per_run": round(sum(r.llm_calls for r in rows) / n, 2) if n else None,
        "input_tokens": sum(r.input_tokens for r in rows),
        "output_tokens": sum(r.output_tokens for r in rows),
        "thinking_tokens": sum(r.thinking_tokens for r in rows),
        "tokens_per_run": round(tokens_total / n, 1) if n else None,
        "estimated_cost_usd": str(cost) if cost is not None else None,
        "cost_per_run_usd": str((cost / n).quantize(Decimal("0.000001")))
        if cost is not None and n
        else None,
        "cost_per_success_usd": str((cost / exact).quantize(Decimal("0.000001")))
        if cost is not None and exact
        else None,
        "actions": sum(r.action_count for r in rows),
        "recoveries": sum(r.recovery_count for r in rows),
        "locator_slips": sum(r.locator_slips for r in rows),
        "policy_blocks": sum(r.policy_blocks for r in rows),
        "side_effect_unknown": sum(r.side_effect == "unknown" for r in rows),
        "duplicate_side_effects": sum(r.duplicate_side_effects for r in rows),
        "unexpected_side_effects": sum(r.unexpected_side_effects for r in rows),
        "cached": sum(r.cached for r in rows),
        "models": sorted({r.model for r in rows if r.model}),
        "outcomes": dict(sorted(_count(r.outcome for r in rows).items())),
    }


def _count(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for v in values:
        out[v] += 1
    return dict(out)


def aggregate(rows: list[RunMetrics], tags: dict[str, list[str]]) -> dict[str, Any]:
    """``tags``: task id → its tags, for the per-tag view (drift, recovery, ...)."""
    by_strategy: dict[str, list[RunMetrics]] = defaultdict(list)
    by_task: dict[tuple[str, str], list[RunMetrics]] = defaultdict(list)
    by_tag: dict[tuple[str, str], list[RunMetrics]] = defaultdict(list)
    for r in rows:
        by_strategy[r.strategy].append(r)
        by_task[(r.task_id, r.strategy)].append(r)
        for tag in tags.get(r.task_id, []):
            by_tag[(tag, r.strategy)].append(r)
    strategies = {s: stats(rs) for s, rs in sorted(by_strategy.items())}
    return {
        "sessions": sorted({r.session_id for r in rows}),
        "runs": len(rows),
        "strategies": strategies,
        "tasks": [
            {"task": t, "strategy": s, **stats(rs)} for (t, s), rs in sorted(by_task.items())
        ],
        "tags": [{"tag": t, "strategy": s, **stats(rs)} for (t, s), rs in sorted(by_tag.items())],
        "break_even": break_even(by_strategy),
    }


def break_even(by_strategy: dict[str, list[RunMetrics]]) -> dict[str, Any] | None:
    """Invocations after which discover-once-then-replay has cost less in
    model spend than asking the model every time:
    ``discovery / (baseline per run - replay per run)``. Model cost only;
    browser time is compared in the latency columns, not priced."""
    base = by_strategy.get("baseline_llm", [])
    disc = by_strategy.get("inter_cua_discovery", [])
    rep = by_strategy.get("inter_cua_replay", [])
    base_cost, disc_cost, rep_cost = _cost(base), _cost(disc), _cost(rep)
    if not base or not disc or not rep or None in (base_cost, disc_cost, rep_cost):
        return None
    if any(r.provider == "scripted" for r in base + disc):
        return None  # a played-back script costs nothing; there is nothing to break even on
    assert base_cost is not None and disc_cost is not None and rep_cost is not None
    per_base = base_cost / len(base)
    per_disc = disc_cost / len(disc)
    per_rep = rep_cost / len(rep)
    saving = per_base - per_rep
    return {
        "baseline_per_run_usd": str(per_base.quantize(Decimal("0.000001"))),
        "discovery_usd": str(per_disc.quantize(Decimal("0.000001"))),
        "replay_per_run_usd": str(per_rep.quantize(Decimal("0.000001"))),
        "invocations": None if saving <= 0 else math.ceil(per_disc / saving),
    }
