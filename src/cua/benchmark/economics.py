"""When discovering a capability once and replaying it costs less than asking
a model every time.

    traditional CUA:  N x baseline per run
    inter-cua:        discovery + N x replay per run
    break-even:       N* = discovery / (baseline per run - replay per run)

Every invocation is priced in three parts:

- ``model``: the row's own estimate, tokens times the price table when it ran;
- ``browser``: its wall clock times the configured price of a browser-hour.
  The baseline holds its browser while it waits on the model, and pays for it;
- ``storage``: the evidence it left (log, observations, screenshots, trace)
  times the configured price of storage over the retention period.

Replay is never taken as free: its browser time and evidence are priced like
anyone's. A part with no price is unpriced, and a total counts only the parts
priced for every strategy it is compared with, and says which.

The comparison is per capability: its discovery against the baseline and
replay runs of the tasks it serves, on the tasks both strategies ran (a task
only one ran would change the mix, not the price). Per-run figures are means
over that mix, so a capability that often meets a failing app is priced with
it. Discovery costs every attempt at it, divided by the attempts that
produced a capability: a failed discovery is money spent getting one.

The range beside N* comes from the 95 % bootstrap interval of the per-run
saving (the statistical report's resamples and seed). Discovery ran once per
capability, so its own spread is not in the range.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from cua.benchmark.inference import BASE, REPLAY, bootstrap_diff_ci
from cua.benchmark.models import RunMetrics, Suite
from cua.observability.cost import InfrastructurePrice, PriceTable

DISCOVERY = "inter_cua_discovery"
PARTS = ("model", "browser", "storage")
VOLUMES = (10, 100, 1_000, 10_000)
GIB = Decimal(1024**3)
HOUR = Decimal(3600)
MICRO = Decimal("0.000001")


def evidence_bytes(row: RunMetrics) -> int | None:
    """Bytes the run stored in its run directory. 0 for a run that stored
    none: an answer served from the idempotency store points at the original
    run's directory, which that run already counted. None when the directory
    is gone."""
    if row.cached or row.run_dir is None:
        return 0
    path = Path(row.run_dir)
    if not path.is_dir():
        return None
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def stored_bytes(row: RunMetrics) -> int | None:
    """As recorded when the run ended; for an older row, measured now from
    its run directory if that is still on disk."""
    return row.evidence_bytes if row.evidence_bytes is not None else evidence_bytes(row)


def part_costs(
    row: RunMetrics, infra: InfrastructurePrice | None, stored: int | None
) -> dict[str, Decimal | None]:
    browser = storage = None
    if infra is not None:
        browser = Decimal(repr(row.duration_s)) / HOUR * infra.browser_hour
        if stored is not None:
            storage = Decimal(stored) / GIB * infra.storage_gb_month * infra.retention_months
    return {"model": row.estimated_cost_usd, "browser": browser, "storage": storage}


class _Group:
    """One strategy's runs for one capability, priced part by part."""

    def __init__(self, rows: list[RunMetrics], infra: InfrastructurePrice | None) -> None:
        self.rows = rows
        stored = [stored_bytes(r) for r in rows]
        self.measured = [b for b in stored if b is not None]
        self.costs = [part_costs(r, infra, b) for r, b in zip(rows, stored, strict=True)]
        self.mean: dict[str, Decimal | None] = {}
        for part in PARTS:
            values = [c[part] for c in self.costs]
            known = [v for v in values if v is not None]
            if part == "storage":
                # A run whose directory is gone is left out of the mean, not
                # counted as storing nothing; the coverage is reported.
                self.mean[part] = _mean(known) if known else None
            else:
                self.mean[part] = _mean(known) if known and len(known) == len(values) else None

    def priced(self) -> set[str]:
        return {p for p, v in self.mean.items() if v is not None}

    def per_run(self, parts: set[str]) -> Decimal:
        return sum((self.mean[p] or Decimal(0) for p in parts), Decimal(0))

    def totals(self, parts: set[str]) -> list[float]:
        """Each run's total over ``parts``, for the bootstrap; a run with an
        unmeasured part takes the group's mean for it."""
        return [
            float(sum((_known(c[p], self.mean[p]) for p in parts), Decimal(0))) for c in self.costs
        ]

    def describe(self) -> dict[str, Any]:
        n = len(self.rows)
        return {
            "runs": n,
            "tasks": sorted({r.task_id for r in self.rows}),
            "duration_mean_s": round(statistics.fmean(r.duration_s for r in self.rows), 3),
            "evidence_bytes_mean": round(statistics.fmean(self.measured))
            if self.measured
            else None,
            "evidence_measured_runs": len(self.measured),
            "per_run_usd": {p: _str(v) for p, v in self.mean.items()},
        }


def analyse(rows: list[RunMetrics], suite: Suite, prices: PriceTable) -> dict[str, Any]:
    from cua.benchmark.report import lost_to_provider

    rows = [r for r in rows if not lost_to_provider(r)]
    infra = prices.infrastructure
    serves = {t.id: Path(t.capability).stem for t in suite.tasks if t.capability}
    by_cap: dict[str, list[RunMetrics]] = defaultdict(list)
    notes: list[str] = []
    for r in rows:
        if r.task_id in serves:
            by_cap[serves[r.task_id]].append(r)
    unmapped = sorted({r.task_id for r in rows} - set(serves))
    if unmapped:
        notes.append(
            "left out, no capability of their own in the suite (a workflow, a task asked of "
            f"the model alone, or not in the suite): {', '.join(unmapped)}"
        )
    capabilities = []
    for cap, cap_rows in sorted(by_cap.items()):
        entry = _capability(cap, cap_rows, infra)
        if entry is not None:
            capabilities.append(entry)
        else:
            notes.append(f"{cap}: needs baseline, discovery and replay runs; not all were run")
    return {
        "sessions": sorted({r.session_id for r in rows}),
        "models": sorted({r.model for r in rows if r.model and r.strategy != REPLAY}),
        "infrastructure": infra.model_dump(mode="json") if infra else None,
        "volumes": list(VOLUMES),
        "capabilities": capabilities,
        "notes": notes,
    }


def _capability(
    cap: str, rows: list[RunMetrics], infra: InfrastructurePrice | None
) -> dict[str, Any] | None:
    base_all = [r for r in rows if r.strategy == BASE]
    rep_all = [r for r in rows if r.strategy == REPLAY]
    disc_rows = [r for r in rows if r.strategy == DISCOVERY]
    both = {r.task_id for r in base_all} & {r.task_id for r in rep_all}
    if not both or not disc_rows:
        return None
    base = _Group([r for r in base_all if r.task_id in both], infra)
    rep = _Group([r for r in rep_all if r.task_id in both], infra)
    disc = _Group(disc_rows, infra)
    scripted = any(r.provider == "scripted" for r in base.rows + disc.rows)
    parts = base.priced() & rep.priced() & disc.priced()
    obtained = sum(r.success for r in disc.rows)
    entry: dict[str, Any] = {
        "capability": cap,
        "tasks": sorted(both),
        "left_out_tasks": sorted({r.task_id for r in base_all + rep_all} - both),
        "priced_parts": [p for p in PARTS if p in parts],
        "scripted": scripted,
        "baseline": base.describe(),
        "replay": rep.describe(),
        "discovery": {**disc.describe(), "obtained": obtained},
    }
    if scripted:
        entry["reason"] = "the baseline was a script, not a model: its model cost is zero"
        return entry
    if not obtained:
        entry["reason"] = "no discovery run produced a capability"
        return entry
    if "model" not in parts:
        entry["reason"] = "a model run is unpriced, so the model cost is unknown"
        return entry
    discovery = disc.per_run(parts) * len(disc.rows) / obtained
    entry["discovery_usd"] = _str(discovery)
    entry["baseline_per_run_usd"] = _str(base.per_run(parts))
    entry["replay_per_run_usd"] = _str(rep.per_run(parts))
    entry["break_even"] = _break_even(discovery, base, rep, parts)
    model_only = {"model"}
    disc_model = disc.per_run(model_only) * len(disc.rows) / obtained
    entry["model_only"] = _break_even(disc_model, base, rep, model_only)
    entry["at_volume"] = [
        _volume(n, discovery, base.per_run(parts), rep.per_run(parts)) for n in VOLUMES
    ]
    return entry


def _break_even(discovery: Decimal, base: _Group, rep: _Group, parts: set[str]) -> dict[str, Any]:
    saving = base.per_run(parts) - rep.per_run(parts)
    ci = bootstrap_diff_ci(base.totals(parts), rep.totals(parts), statistics.fmean)
    out: dict[str, Any] = {
        "saving_per_run_usd": _str(saving),
        "saving_ci95": list(ci) if ci else None,
        "invocations": math.ceil(discovery / saving) if saving > 0 else None,
        "exact": round(float(discovery / saving), 3) if saving > 0 else None,
    }
    if ci is not None:
        lo, hi = ci
        # The widest saving gives the earliest break-even; a saving that may
        # be zero or less leaves it unbounded (None).
        out["invocations_ci95"] = [
            math.ceil(float(discovery) / hi) if hi > 0 else None,
            math.ceil(float(discovery) / lo) if lo > 0 else None,
        ]
    return out


def _volume(n: int, discovery: Decimal, base: Decimal, rep: Decimal) -> dict[str, Any]:
    traditional = base * n
    ours = discovery + rep * n
    return {
        "invocations": n,
        "traditional_usd": _str(traditional),
        "inter_cua_usd": _str(ours),
        "saved_usd": _str(traditional - ours),
        "inter_cua_share": round(float(ours / traditional), 4) if traditional else None,
    }


def _known(value: Decimal | None, fallback: Decimal | None) -> Decimal:
    return value if value is not None else fallback or Decimal(0)


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(MICRO))


# -- report ---------------------------------------------------------------------


def write(result: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    js = out_dir / "break_even.json"
    md = out_dir / "break_even.md"
    js.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    md.write_text(markdown(result), encoding="utf-8", newline="\n")
    return js, md


def _usd(value: str | float | Decimal | None, places: int = 6) -> str:
    if value is None:
        return "unpriced"
    v = Decimal(str(value))
    return f"-${-v:,.{places}f}" if v < 0 else f"${v:,.{places}f}"


def _n(value: int | None) -> str:
    return "never" if value is None else f"{value:,}"


def markdown(result: dict[str, Any]) -> str:
    infra = result.get("infrastructure")
    lines = [
        "# Cost break-even",
        "",
        "When discovering a capability once and replaying it costs less than asking a "
        "model to operate the UI on every invocation.",
        "",
        "```text",
        "traditional CUA:  N x baseline per run",
        "inter-cua:        discovery + N x replay per run",
        "break-even:       N* = discovery / (baseline per run - replay per run)",
        "```",
        "",
        f"Sessions: {', '.join(f'`{s}`' for s in result['sessions'])}. "
        f"Models: {', '.join(f'`{m}`' for m in result['models']) or 'none'}.",
        "",
        "## Prices",
        "",
        "- **Model**: each run's own estimate, its tokens times `bench/pricing.yaml` when it ran.",
    ]
    if infra:
        lines += [
            f"- **Browser**: ${infra['browser_hour']} per browser-hour, times each run's wall "
            "clock. The baseline holds its browser while it waits on the model.",
            f"- **Storage**: ${infra['storage_gb_month']} per GiB-month for "
            f"{infra['retention_months']} month(s), times the evidence each run left (log, "
            "observations, screenshots, trace).",
            f"- Source: {infra['source'] or 'not given'}"
            + (f" (as of {infra['as_of']})" if infra.get("as_of") else "")
            + ". Configured, not measured; the seconds and bytes they multiply are measured.",
        ]
    else:
        lines.append(
            "- **Browser and storage are not priced**: the price table has no "
            "`infrastructure` section, so every total below is model spend only."
        )
    lines.append("")
    caps = result["capabilities"]
    lines += _per_run(caps)
    lines += _break_even_table(caps)
    lines += _volumes(caps)
    lines += [
        "## What this leaves out",
        "",
        "- **A person's review.** A discovered capability is approved by a person before it "
        "replays. That is a one-time cost per capability version, not priced here.",
        "- **Re-discovery after drift.** When the UI changes, replay stops safely "
        "(`LOCATOR_UNRESOLVED`) and the capability has to be discovered again: one more "
        "discovery cost each time. Re-discovered every K invocations, a capability adds "
        "discovery / K to each one.",
        "- **What follows a safe stop.** A run that stopped, and handed its work to a person, "
        "is priced as the run it was. The person's time is not in it.",
        "- **Prices move.** Model prices come from the table with their date; the host and "
        "storage prices are configured. These are estimates, not billing.",
        "- **One mock app on one machine.** The seconds and bytes are this app's and this "
        "machine's; another application moves every number.",
        "",
    ]
    if result.get("notes"):
        lines += ["## Notes", "", *(f"- {n}" for n in result["notes"]), ""]
    return "\n".join(lines)


LABEL = {BASE: "Repeated LLM (baseline)", DISCOVERY: "Discovery (once)", REPLAY: "inter-cua replay"}


def _per_run(caps: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## Per invocation",
        "",
        "Mean cost per run, by part. Discovery is per capability obtained: every attempt, "
        "divided by the attempts that produced one.",
        "",
        "| Capability | Strategy | Runs | Mean s | Evidence KiB/run | Model | Browser | Storage |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for c in caps:
        for key, strategy in (("baseline", BASE), ("discovery", DISCOVERY), ("replay", REPLAY)):
            g = c[key]
            kib = g["evidence_bytes_mean"]
            coverage = (
                f" ({g['evidence_measured_runs']} of {g['runs']} measured)"
                if kib is not None and g["evidence_measured_runs"] < g["runs"]
                else ""
            )
            per = g["per_run_usd"]
            lines.append(
                f"| {c['capability']} | {LABEL[strategy]} | {g['runs']} | "
                f"{g['duration_mean_s']:.2f} | "
                + ("-" if kib is None else f"{kib / 1024:,.0f}{coverage}")
                + f" | {_usd(per['model'])} | {_usd(per['browser'])} | {_usd(per['storage'])} |"
            )
    lines += [""]
    tasks = [
        f"- {c['capability']}: {', '.join(c['tasks'])}"
        + (
            f" (left out, one strategy only: {', '.join(c['left_out_tasks'])})"
            if c["left_out_tasks"]
            else ""
        )
        for c in caps
    ]
    if tasks:
        lines += ["Tasks in each capability's mix (both strategies ran them):", "", *tasks, ""]
    return lines


def _break_even_table(caps: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## Break-even",
        "",
        "| Capability | Priced | Discovery | Baseline/run | Replay/run | Saving/run (95% CI) | "
        "N* (95% range) | Model spend only |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    reasons = []
    for c in caps:
        if "break_even" not in c:
            reasons.append(f"- {c['capability']}: not calculated, {c.get('reason', 'no data')}.")
            continue
        be, mo = c["break_even"], c["model_only"]
        ci = be.get("saving_ci95")
        span = be.get("invocations_ci95")
        lines.append(
            f"| {c['capability']} | {' + '.join(c['priced_parts'])} | {_usd(c['discovery_usd'])} | "
            f"{_usd(c['baseline_per_run_usd'])} | {_usd(c['replay_per_run_usd'])} | "
            f"{_usd(be['saving_per_run_usd'])}"
            + (f" ({_usd(ci[0])} to {_usd(ci[1])})" if ci else "")
            + f" | **{_n(be['invocations'])}**"
            + (f" ({_n(span[0])} to {_n(span[1])})" if span else "")
            + f" | {_n(mo['invocations'])} |"
        )
    lines += ["", *reasons] if reasons else [""]
    lines += [
        "N* is the first whole number of invocations after which discover-then-replay has "
        "spent less in total than asking the model every time (the exact ratio is in "
        "`break_even.json`). *Model spend only* is the same calculation on the model part "
        "alone. The range comes from the bootstrap interval of the saving per run; "
        "discovery ran once, so its own spread is not in it.",
        "",
    ]
    return lines


def _volumes(caps: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## At volume",
        "",
        "| Capability | Invocations | Traditional CUA | inter-cua | Saved | "
        "inter-cua / traditional |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for c in caps:
        for v in c.get("at_volume", []):
            share = v["inter_cua_share"]
            lines.append(
                f"| {c['capability']} | {v['invocations']:,} | {_usd(v['traditional_usd'], 2)} | "
                f"{_usd(v['inter_cua_usd'], 2)} | {_usd(v['saved_usd'], 2)} | "
                + ("-" if share is None else f"{share * 100:.1f}%")
                + " |"
            )
    lines.append("")
    return lines
