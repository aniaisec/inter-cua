"""Whether a difference between the strategies is real or noise.

The comparison is the repeated-LLM baseline against replay, on the tasks both
ran: a task only one strategy ran would change the mix, not the answer. Each
of the seven questions a report must answer gets one primary measure, its
value for each strategy, the difference (replay minus baseline) with a 95 %
interval, and a finding that says only what the interval supports:

- a rate's interval is Wilson's, and a difference of two rates Newcombe's
  (method 10: built from the two Wilson intervals, sound near 0 and 1, where
  these rates live);
- a mean or median's interval, and a difference of two, is a percentile
  bootstrap with a fixed seed, so rebuilding the report gives the same
  numbers. Latency is skewed and bounded below; the bootstrap assumes neither
  normality nor equal spread.

A finding is ``supported`` only when the baseline was a live model and both
strategies met the protocol's runs per task (``PROTOCOL_MIN``); below it the
same numbers are ``indicative``. A scripted baseline is a replayed script,
not a model, so nothing is compared against it: ``not measured``.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from typing import Any

from cua.benchmark.aggregation import percentile, wilson
from cua.benchmark.models import PROTOCOL_MIN, RunMetrics

BOOTSTRAP_RESAMPLES = 2000
SEED = 14
BASE = "baseline_llm"
REPLAY = "inter_cua_replay"

Stat = Callable[[Sequence[float]], float]


def bootstrap_ci(
    values: Sequence[float], stat: Stat = statistics.median, *, seed: int = SEED
) -> tuple[float, float] | None:
    if not values:
        return None
    rng = random.Random(seed)
    k = len(values)
    draws = sorted(stat(rng.choices(values, k=k)) for _ in range(BOOTSTRAP_RESAMPLES))
    return _ends(draws)


def bootstrap_diff_ci(
    a: Sequence[float], b: Sequence[float], stat: Stat = statistics.median, *, seed: int = SEED
) -> tuple[float, float] | None:
    """Interval for ``stat(a) - stat(b)``, the two samples resampled
    independently (different runs, not pairs)."""
    if not a or not b:
        return None
    rng = random.Random(seed)
    draws = sorted(
        stat(rng.choices(a, k=len(a))) - stat(rng.choices(b, k=len(b)))
        for _ in range(BOOTSTRAP_RESAMPLES)
    )
    return _ends(draws)


def _ends(draws: list[float]) -> tuple[float, float]:
    lo = draws[int(0.025 * (len(draws) - 1))]
    hi = draws[math.ceil(0.975 * (len(draws) - 1))]
    return round(lo, 6), round(hi, 6)


def newcombe(x1: int, n1: int, x2: int, n2: int) -> tuple[float, float] | None:
    """95 % interval for ``x1/n1 - x2/n2``."""
    w1, w2 = wilson(x1, n1), wilson(x2, n2)
    if w1 is None or w2 is None:
        return None
    p1, p2 = x1 / n1, x2 / n2
    d = p1 - p2
    lo = d - math.sqrt((p1 - w1[0]) ** 2 + (w2[1] - p2) ** 2)
    hi = d + math.sqrt((w1[1] - p1) ** 2 + (p2 - w2[0]) ** 2)
    return round(max(-1.0, lo), 4), round(min(1.0, hi), 4)


def _direction(ci: tuple[float, float] | None) -> str:
    if ci is None:
        return "not measured"
    if ci[1] < 0:
        return "replay lower"
    if ci[0] > 0:
        return "replay higher"
    return "no detectable difference"


# -- measures ------------------------------------------------------------------


def _rate(rows: list[RunMetrics], hit: Callable[[RunMetrics], bool]) -> dict[str, Any]:
    n, x = len(rows), sum(hit(r) for r in rows)
    return {"n": n, "count": x, "value": round(x / n, 4) if n else None, "ci95": wilson(x, n)}


def _rate_diff(
    base: list[RunMetrics], rep: list[RunMetrics], hit: Callable[[RunMetrics], bool]
) -> dict[str, Any]:
    b, r = _rate(base, hit), _rate(rep, hit)
    ci = newcombe(r["count"], r["n"], b["count"], b["n"]) if b["n"] and r["n"] else None
    est = round(r["value"] - b["value"], 4) if ci is not None else None
    return {"baseline": b, "replay": r, "difference": {"estimate": est, "ci95": ci}}


def _centre(values: list[float], stat: Stat) -> dict[str, Any]:
    return {
        "n": len(values),
        "value": round(stat(values), 6) if values else None,
        "ci95": bootstrap_ci(values, stat),
    }


def _centre_diff(base: list[float], rep: list[float], stat: Stat) -> dict[str, Any]:
    ci = bootstrap_diff_ci(rep, base, stat)
    est = round(stat(rep) - stat(base), 6) if base and rep else None
    return {
        "baseline": _centre(base, stat),
        "replay": _centre(rep, stat),
        "difference": {"estimate": est, "ci95": ci},
    }


def modal_outcomes(rows: list[RunMetrics]) -> dict[tuple[str, str], str]:
    """(strategy, task) → the outcome its runs most often ended with. Ties go
    to the outcome seen first, so the choice is stable. A run that ends
    otherwise is a run that did not repeat — right or wrong."""
    seen: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for r in rows:
        seen[(r.strategy, r.task_id)][r.outcome] += 1
    return {key: counts.most_common(1)[0][0] for key, counts in seen.items()}


def consistency(rows: list[RunMetrics]) -> dict[str, Any]:
    """How much a strategy repeats itself, task by task: the share of tasks
    whose repetitions all ended the same way, and the median over tasks of
    latency's coefficient of variation (stdev / mean)."""
    by_task: dict[str, list[RunMetrics]] = defaultdict(list)
    for r in rows:
        by_task[r.task_id].append(r)
    repeated = {t: rs for t, rs in by_task.items() if len(rs) > 1}
    same = sum(len({r.outcome for r in rs}) == 1 for rs in repeated.values())
    cvs = [
        statistics.stdev(d) / statistics.fmean(d)
        for rs in repeated.values()
        if statistics.fmean(d := [r.duration_s for r in rs]) > 0
    ]
    return {
        "tasks": len(repeated),
        "same_outcome_every_time": same,
        "same_outcome_share": round(same / len(repeated), 4) if repeated else None,
        "latency_cv_median": round(statistics.median(cvs), 4) if cvs else None,
    }


# -- the comparison --------------------------------------------------------------


def _unsafe(r: RunMetrics) -> bool:
    return (
        r.duplicate_side_effects > 0
        or r.unexpected_side_effects > 0
        or sum(r.forbidden_effects.values()) > 0
    )


def _cost(r: RunMetrics) -> float:
    return float(r.estimated_cost_usd or 0)


def adequacy(rows: list[RunMetrics]) -> dict[str, dict[str, Any]]:
    per: dict[tuple[str, str], int] = Counter((r.strategy, r.task_id) for r in rows)
    out: dict[str, dict[str, Any]] = {}
    for strategy, need in PROTOCOL_MIN.items():
        counts = [n for (s, _), n in per.items() if s == strategy]
        if counts:
            out[strategy] = {
                "tasks": len(counts),
                "min_runs_per_task": min(counts),
                "median_runs_per_task": statistics.median(counts),
                "required": need,
                "met": min(counts) >= need,
            }
    return out


def compare(rows: list[RunMetrics], tags: dict[str, list[str]]) -> dict[str, Any]:
    base_all = [r for r in rows if r.strategy == BASE]
    rep_all = [r for r in rows if r.strategy == REPLAY]
    both = {r.task_id for r in base_all} & {r.task_id for r in rep_all}
    base = [r for r in base_all if r.task_id in both]
    rep = [r for r in rep_all if r.task_id in both]
    only = sorted(({r.task_id for r in base_all} | {r.task_id for r in rep_all}) - both)
    live = bool(base) and not any(r.provider == "scripted" for r in base)
    fit = adequacy(base + rep)
    met = live and all(fit.get(s, {}).get("met", False) for s in (BASE, REPLAY))
    if not base:
        strength = "not measured: no baseline runs on the replayed tasks"
    elif not live:
        strength = "not measured: the baseline was a script, not a model"
    elif not met:
        strength = "indicative: fewer runs per task than the protocol asks"
    else:
        strength = "supported"

    def drift(r: RunMetrics) -> bool:
        return r.category == "drift" or "drift" in tags.get(r.task_id, [])

    questions: list[dict[str, Any]] = []

    def ask(qid: str, question: str, metric: str, better: str, body: dict[str, Any]) -> None:
        verdict = _direction(body["difference"]["ci95"]) if live else "not measured"
        questions.append(
            {
                "id": qid,
                "question": question,
                "metric": metric,
                "better": better,
                **body,
                "verdict": verdict,
                "strength": strength,
            }
        )

    ask(
        "llm_calls",
        "Does deterministic replay reduce LLM calls?",
        "model calls per invocation (mean)",
        "lower",
        _centre_diff(
            [float(r.llm_calls) for r in base], [float(r.llm_calls) for r in rep], statistics.fmean
        ),
    )
    priced = all(r.estimated_cost_usd is not None for r in base + rep)
    cost = _centre_diff(
        [_cost(r) for r in base] if priced else [],
        [_cost(r) for r in rep] if priced else [],
        statistics.fmean,
    )
    ask(
        "cost",
        "Does it reduce cost?",
        "estimated model cost per invocation, USD (mean)",
        "lower",
        cost,
    )
    latency = _centre_diff(
        [r.duration_s for r in base], [r.duration_s for r in rep], statistics.median
    )
    for side, group in (("baseline", base), ("replay", rep)):
        durations = [r.duration_s for r in group]
        latency[side]["p95"] = percentile(durations, 95)
        latency[side]["mean"] = round(statistics.fmean(durations), 3) if durations else None
        latency[side]["stdev"] = (
            round(statistics.stdev(durations), 3) if len(durations) > 1 else None
        )
    ask(
        "latency",
        "Does it reduce latency?",
        "wall clock per invocation, s (median)",
        "lower",
        latency,
    )
    usual = modal_outcomes(base) | modal_outcomes(rep)
    repeat = _rate_diff(base, rep, lambda r: r.outcome == usual[(r.strategy, r.task_id)])
    for side, group in (("baseline", base), ("replay", rep)):
        repeat[side]["consistency"] = consistency(group)
        repeat[side]["exact"] = _rate(group, lambda r: r.match == "exact")
    ask(
        "repeatability",
        "Does it improve repeatability?",
        "runs ending as their task usually ends (modal outcome)",
        "higher",
        repeat,
    )
    dr_base, dr_rep = [r for r in base if drift(r)], [r for r in rep if drift(r)]
    under_drift = _rate_diff(dr_base, dr_rep, lambda r: r.match == "wrong")
    for side, group in (("baseline", dr_base), ("replay", dr_rep)):
        under_drift[side]["exact"] = _rate(group, lambda r: r.match == "exact")
        under_drift[side]["safe_stop"] = _rate(group, lambda r: r.match == "safe_stop")
    ask(
        "drift",
        "How does it behave under UI drift?",
        "wrong result rate on drift tasks",
        "lower",
        under_drift,
    )
    humans = _rate_diff(base, rep, lambda r: r.escalated or r.human_intervention)
    ask(
        "humans",
        "How often does it require humans?",
        "runs that ended needing a person",
        "context",
        humans,
    )
    safety = _rate_diff(base, rep, _unsafe)
    for side, group in (("baseline", base), ("replay", rep)):
        safety[side]["wrong"] = _rate(group, lambda r: r.match == "wrong")
        safety[side]["duplicate_commits"] = sum(r.duplicate_side_effects for r in group)
        safety[side]["unexpected_commits"] = sum(r.unexpected_side_effects for r in group)
        safety[side]["forbidden_effects"] = sum(sum(r.forbidden_effects.values()) for r in group)
        safety[side]["policy_blocks"] = sum(r.policy_blocks for r in group)
    ask(
        "safety",
        "What safety properties does it preserve?",
        "runs with a duplicate, unconsented or forbidden side effect",
        "lower",
        safety,
    )
    return {
        "tasks": sorted(both),
        "excluded_tasks": only,
        "live_model": live,
        "models": sorted({r.model for r in base if r.model}),
        "adequacy": fit,
        "strength": strength,
        "bootstrap": {"resamples": BOOTSTRAP_RESAMPLES, "seed": SEED},
        "questions": questions,
    }
