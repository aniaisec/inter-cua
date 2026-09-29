"""What people spent on the runs: how often a run needed one, what they
did, and how long it took them.

Every request to a person is one intervention, classified by how it ended
(``cua.observability.metrics.INTERVENTION_KINDS``): an approval, a recovery,
a manual completion and an abort are different work and are never pooled
into one number. Time is the run's time on the person, from the control
transitions: *queued* until someone took the request, *in control* while
they held it. An approval from the console takes no control time; its
queue is the reading and deciding.

``runs requiring a person`` counts every run that asked for one or ended
needing one, answered or not; ``intervention rate`` counts the runs where a
person acted. The difference is the work left for someone after the run.

Who decided is kept beside each kind. A scripted operator (the evidence
builder's) answers in a fraction of a second, which is not a person's time:
read its rows as the mechanism working, not as a measurement of people.
"""

from __future__ import annotations

import statistics
from collections import Counter
from decimal import Decimal
from typing import Any

from cua.observability.cost import HumanPrice
from cua.observability.health import percentile
from cua.observability.metrics import DECIDED, INTERVENTION_KINDS, Intervention, RunMetrics

KINDS = ("replay", "discovery")
HOUR = Decimal(3600)


def needs_human(r: RunMetrics) -> bool:
    return r.status == "escalated" or r.human.handoffs > 0


def summarise(runs: list[RunMetrics], price: HumanPrice | None = None) -> dict[str, Any]:
    groups = {"all": runs, **{k: [r for r in runs if r.kind == k] for k in KINDS}}
    return {
        "runs": len(runs),
        "operator_hour_usd": str(price.operator_hour) if price else None,
        "groups": {name: _group(rs, price) for name, rs in groups.items() if rs},
        "interventions": [
            {"run_id": r.run_id, "kind_of_run": r.kind, "capability": r.capability, **_dump(i)}
            for r in runs
            for i in r.human.interventions
        ],
    }


def _dump(i: Intervention) -> dict[str, Any]:
    return {**i.model_dump(mode="json"), "human_s": i.human_s}


def _group(runs: list[RunMetrics], price: HumanPrice | None) -> dict[str, Any]:
    n = len(runs)
    items = [i for r in runs for i in r.human.interventions]
    kinds: dict[str, Any] = {}
    for kind in INTERVENTION_KINDS:
        mine = [i for i in items if i.kind == kind]
        if mine:
            kinds[kind] = _times(mine, price) | {
                "decided_by": dict(Counter(i.decided_by or "-" for i in mine).most_common()),
            }
    decided = [i for i in items if i.kind in DECIDED]
    deciders: dict[str, Any] = {}
    for who in sorted({i.decided_by for i in decided if i.decided_by}):
        mine = [i for i in decided if i.decided_by == who]
        deciders[who] = _times(mine, price) | {
            "kinds": dict(Counter(i.kind for i in mine).most_common())
        }
    return {
        "runs": n,
        "runs_requiring_human": sum(needs_human(r) for r in runs),
        "runs_with_intervention": sum(r.human.intervened for r in runs),
        "human_intervention_rate": round(sum(r.human.intervened for r in runs) / n, 4),
        "requiring_human_rate": round(sum(needs_human(r) for r in runs) / n, 4),
        "human_interventions": len(items),
        "human_wait_seconds": round(sum(r.human.wait_s for r in runs), 3),
        "human_action_count": sum(r.human.browser_actions for r in runs),
        "decided": _times(decided, price) if decided else None,
        "by_kind": kinds,
        "by_decider": deciders,
    }


def _times(items: list[Intervention], price: HumanPrice | None) -> dict[str, Any]:
    human = [i.human_s for i in items]
    out: dict[str, Any] = {
        "count": len(items),
        "mean_human_s": round(statistics.fmean(human), 3),
        "median_human_s": round(statistics.median(human), 3),
        "p95_human_s": percentile(human, 95),
        "mean_queued_s": round(statistics.fmean(i.queued_s for i in items), 3),
        "mean_in_control_s": round(statistics.fmean(i.in_control_s for i in items), 3),
        "browser_actions": sum(i.browser_actions for i in items),
    }
    if price is not None:
        mean = Decimal(repr(out["mean_human_s"]))
        out["mean_cost_usd"] = str((mean / HOUR * price.operator_hour).quantize(Decimal("0.0001")))
    return out


LABEL = {
    "approval": "approval",
    "recovery": "recovery (the automation carried on)",
    "manual_completion": "manual completion (the person finished)",
    "abort": "abort (a person stopped it)",
    "expired": "expired (nobody answered in time)",
    "unanswered": "unanswered (suspended for `cua resume`)",
    "returned": "returned (handed back, no checkpoint held; asked again)",
    "not_asked": "not asked (no channel to a person)",
    "open": "open (the run ended with it open)",
}


def markdown(data: dict[str, Any]) -> list[str]:
    lines = ["## Human intervention", ""]
    if not data["groups"]:
        return [*lines, "No runs.", ""]
    priced = data.get("operator_hour_usd")
    lines += [
        "| Run kind | Runs | Requiring a person | A person acted | Interventions | "
        "Human wait s | Browser actions |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, g in data["groups"].items():
        lines.append(
            f"| {name} | {g['runs']} | {g['runs_requiring_human']} "
            f"({g['requiring_human_rate'] * 100:.1f}%) | {g['runs_with_intervention']} "
            f"({g['human_intervention_rate'] * 100:.1f}%) | {g['human_interventions']} | "
            f"{g['human_wait_seconds']:.1f} | {g['human_action_count']} |"
        )
    g = data["groups"]["all"]
    if g["by_kind"]:
        lines += [
            "",
            "| How it ended | Count | Mean s | Median s | P95 s | Queued s (mean) | "
            "In control s (mean) | Browser actions | Decided by"
            + (" | Mean cost |" if priced else " |"),
            "|---|---:|---:|---:|---:|---:|---:|---:|---|" + ("---:|" if priced else ""),
        ]
        for kind, k in g["by_kind"].items():
            who = ", ".join(f"{b} x{c}" for b, c in k["decided_by"].items())
            lines.append(
                f"| {LABEL[kind]} | {k['count']} | {k['mean_human_s']:.2f} | "
                f"{k['median_human_s']:.2f} | {k['p95_human_s']:.2f} | {k['mean_queued_s']:.2f} | "
                f"{k['mean_in_control_s']:.2f} | {k['browser_actions']} | {who}"
                + (f" | ${k['mean_cost_usd']} |" if priced else " |")
            )
    d = g.get("decided")
    if d:
        lines += [
            "",
            f"Answered by someone ({d['count']}): mean {d['mean_human_s']:.2f} s, "
            f"p95 {d['p95_human_s']:.2f} s of the run's time on them"
            + (f"; about ${d['mean_cost_usd']} each at ${priced}/h" if priced else "")
            + ".",
            "",
            "| Decided by | Count | Mean s | P95 s | How it ended |",
            "|---|---:|---:|---:|---|",
        ]
        for who, w in g["by_decider"].items():
            ended = ", ".join(f"{k} x{c}" for k, c in w["kinds"].items())
            lines.append(
                f"| {who} | {w['count']} | {w['mean_human_s']:.2f} | {w['p95_human_s']:.2f} | "
                f"{ended} |"
            )
    lines += [
        "",
        "Time is the run's time on the person, from its control transitions: queued until "
        "someone took the request, in control while they held it. An unanswered request "
        "counts until the run was resumed. A person acted means a browser action, a handback "
        "or an abort; a run requiring a person asked for one or ended needing one. Rows "
        "decided by a scripted operator show the mechanism, not a person's time.",
        "",
    ]
    return lines
