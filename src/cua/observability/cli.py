"""``cua metrics run | capability | report``.

Reads run directories; writes nothing unless ``--out`` is given. Imports no
model client: explaining a replay must not be able to reach one.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

EX_USAGE = 64
DEFAULT_ROOTS = (Path("evidence"), Path("bench/runs"))


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    m = sub.add_parser(
        "metrics",
        help="Explain a run, and how each capability is doing, from the evidence on disk",
        description="Read run directories (default: under evidence/ and bench/runs/) into "
        "canonical events and explain them: what happened and why, where the time went, "
        "model calls and estimated cost, locators, recoveries, human intervention. Costs "
        "come from the price table and are estimates, not billing data.",
    )
    msub = m.add_subparsers(dest="metrics_command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--runs-dir",
            type=Path,
            action="append",
            default=[],
            help="Where to look for runs (repeatable; default: evidence/ and bench/runs/)",
        )
        p.add_argument("--pricing", type=Path, default=Path("bench/pricing.yaml"))
        p.add_argument("--json", action="store_true", help="Machine-readable output")

    r = msub.add_parser("run", help="One run: what happened, why, and what it cost")
    r.add_argument("run", help="A run id (run_...) or a run directory")
    r.add_argument("--events", action="store_true", help="Print its canonical events (JSONL)")
    common(r)

    c = msub.add_parser("capability", help="Health of one capability, from its replays")
    c.add_argument("name")
    c.add_argument("--version", type=int, help="Only this version (default: each version)")
    c.add_argument(
        "--include-injected",
        action="store_true",
        help="Count runs with a failure injected into the target app",
    )
    common(c)

    rep = msub.add_parser("report", help="Every capability's health, and model spend")
    rep.add_argument("--include-injected", action="store_true")
    rep.add_argument("--out", type=Path, help="Also write metrics.json and metrics.md here")
    common(rep)


def main(args: argparse.Namespace) -> int:
    from cua.observability.cost import load_prices

    roots: list[Path] = list(args.runs_dir) or list(DEFAULT_ROOTS)
    prices = load_prices(args.pricing)
    if args.metrics_command == "run":
        return _run(args, roots, prices)
    if args.metrics_command == "capability":
        return _capability(args, roots, prices)
    return _report(args, roots, prices)


# -- run -----------------------------------------------------------------------


def _run(args: argparse.Namespace, roots: list[Path], prices: Any) -> int:
    from cua.observability.metrics import run_metrics
    from cua.observability.recorder import locate_run, read_run

    path = locate_run(args.run, roots)
    if path is None:
        where = ", ".join(r.as_posix() for r in roots)
        print(f"cua metrics: no run {args.run} under {where}", file=sys.stderr)
        return EX_USAGE
    record = read_run(path, prices)
    if args.events:
        for e in record.events:
            print(e.model_dump_json(exclude_defaults=True))
        return 0
    m = run_metrics(record)
    if args.json:
        print(json.dumps({"run_dir": path.as_posix(), **m.model_dump(mode="json")}, indent=2))
        return 0
    print(explain(m, path))
    return 0


def explain(m: Any, path: Path) -> str:
    """The run, in the order a person asks about it."""
    what = (
        f"replay {m.capability} v{m.capability_version}"
        if m.kind == "replay"
        else f"discovery {m.capability or ''}".rstrip()
    )
    lines = [
        f"{m.run_id}  {what}, tenant {m.tenant_id or '?'}",
        f"  dir        {path.as_posix()}",
        f"  started    {m.started_at}",
    ]
    if m.invocation_id != m.run_id:
        lines.append(f"  invocation {m.invocation_id}")
    if m.inject:
        lines.append(f"  injected   {m.inject} (a fault armed in the target app)")
    lines.append(f"  outcome    {m.outcome} ({m.status}) in {m.duration_s:.2f} s")
    if m.why:
        lines.append(f"  why        {m.why}")
    if m.failed_step:
        lines.append(f"  failed at  {m.failed_step}")
    llm = m.llm
    if llm.calls or llm.errors:
        cost = "unpriced" if llm.estimated_cost_usd is None else f"~${llm.estimated_cost_usd}"
        waited = f"{llm.wait_s:.2f} s" + ("" if llm.wait_measured else " (inferred)")
        lines.append(
            f"  model      {llm.calls} call(s) to {', '.join(llm.models) or '?'}; "
            f"{llm.input_tokens:,} in / {llm.output_tokens + llm.thinking_tokens:,} out tokens; "
            f"waited {waited}; {cost} (estimate)"
            + (f"; {llm.errors} failed call(s)" if llm.errors else "")
        )
    else:
        lines.append("  model      no calls")
    busy = {k: v for k, v in m.time.items() if v >= 0.005}
    if busy:
        lines.append(
            "  time       "
            + ", ".join(f"{k} {v:.2f}s ({v / m.duration_s:.0%})" for k, v in busy.items())
            if m.duration_s
            else "  time       -"
        )
    lines.append(
        f"  locators   {len(m.locators)} found; {m.locator_fallbacks} by a fallback rung, "
        f"{m.locator_drift} drift, {m.locator_failures} not found"
    )
    rec = f"{m.recoveries}" + (", exhausted" if m.recovery_exhausted else "")
    lines.append(f"  recovery   {rec if m.recoveries else 'none'}")
    lines.append(f"  policy     {m.policy_checks} approval(s) checked, {m.policy_blocks} block(s)")
    h = m.human
    lines.append(
        "  human      "
        + (
            f"{h.handoffs} handoff(s), {h.actions} action(s), {h.resumed} handed back, "
            f"{h.aborted} aborted; waited {h.wait_s:.1f} s"
            if h.handoffs or h.intervened
            else "none"
        )
    )
    lines.append(f"  side effect {m.side_effect or 'not reported'}")
    if m.steps:
        lines += [
            "",
            "  step                              action  rung         status      "
            "total   locate  act     verify",
        ]
        for s in m.steps:
            rung = (s.rung or "-") + ("*" if s.fell_back else "")
            lines.append(
                f"  {s.step_id[:32]:32}  {(s.action or '-')[:6]:6}  {rung[:11]:11}  "
                f"{s.status + (':' + s.error_code if s.error_code else ''):10}  "
                f"{s.duration_s:6.2f}  {s.locate_s:6.2f}  {s.act_s:6.2f}  {s.verify_s:6.2f}"
            )
        if any(s.fell_back for s in m.steps):
            lines.append("  * found by a rung other than the recorded one")
    for w in m.warnings:
        lines.append(f"  warning    {w}")
    return "\n".join(lines)


# -- capability ------------------------------------------------------------------


def _load_all(roots: list[Path], prices: Any) -> list[Any]:
    from cua.observability.correlation import HeaderError
    from cua.observability.metrics import run_metrics
    from cua.observability.recorder import find_runs, read_run

    out = []
    for path in find_runs(roots):
        try:
            out.append(run_metrics(read_run(path, prices)))
        except (HeaderError, ValueError) as exc:
            print(f"cua metrics: skipped {path.as_posix()}: {exc}", file=sys.stderr)
    return out


def _capability(args: argparse.Namespace, roots: list[Path], prices: Any) -> int:
    from cua.observability.health import capability_health

    runs = _load_all(roots, prices)
    if args.version is not None:
        wanted: list[int | None] = [args.version]
    else:
        versions = {
            r.capability_version
            for r in runs
            if r.kind == "replay" and r.capability == args.name and r.capability_version is not None
        }
        wanted = sorted(versions, key=lambda v: v or 0)
        if len(wanted) > 1:
            wanted.append(None)  # and every version together
    found = [
        h
        for v in wanted
        if (
            h := capability_health(
                runs, args.name, version=v, include_injected=args.include_injected
            )
        )
        is not None
    ]
    if not found:
        print(f"cua metrics: no replay of {args.name} on record", file=sys.stderr)
        return EX_USAGE
    if args.json:
        print(json.dumps([h.model_dump(mode="json") for h in found], indent=2))
        return 0
    print("\n".join(health_table(found)))
    return 0


def health_table(rows: Iterable[Any]) -> list[str]:
    lines = [
        "| Capability | Version | Runs | Success | Failure | Escalation | Human | "
        "Locator failure | Drift | Side effect unknown | Mean s | P95 s | Last success | "
        "Last failure |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for h in rows:
        lines.append(
            f"| {h.capability} | {'all' if h.version is None else h.version} | {h.runs}"
            + (f" (+{h.injected_excluded} injected)" if h.injected_excluded else "")
            + (f" (+{h.refused} refused)" if h.refused else "")
            + f" | {_pct(h.success_rate)} | {_pct(h.failure_rate)} | "
            f"{_pct(h.escalation_rate)} | {_pct(h.human_intervention_rate)} | "
            f"{_pct(h.locator_failure_rate)} | {_pct(h.drift_rate)} | "
            f"{_pct(h.side_effect_unknown_rate)} | {h.mean_latency_s:.2f} | "
            f"{h.p95_latency_s:.2f} | {_when(h.last_success)} | {_when(h.last_failure)} |"
        )
    failing = [h for h in rows if h.failures]
    for h in failing:
        what = ", ".join(f"{k} x{v}" for k, v in h.failures.items())
        version = "all versions" if h.version is None else f"v{h.version}"
        lines.append(f"\n{h.capability} {version} did not complete: {what}")
    return lines


def _pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def _when(ref: Any) -> str:
    return "-" if ref is None else f"{ref.at[:19]}Z `{ref.run_id}`"


# -- report ----------------------------------------------------------------------


def _report(args: argparse.Namespace, roots: list[Path], prices: Any) -> int:
    from cua.observability.health import all_health

    runs = _load_all(roots, prices)
    if not runs:
        print("cua metrics: no runs found", file=sys.stderr)
        return EX_USAGE
    health = all_health(runs, include_injected=args.include_injected)
    spend = model_spend(runs)
    data = {
        "roots": [r.as_posix() for r in roots],
        "runs": len(runs),
        "health": [h.model_dump(mode="json") for h in health],
        "model_spend": spend,
    }
    md = report_markdown(data, health)
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "metrics.json").write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        (args.out / "metrics.md").write_text(md, encoding="utf-8", newline="\n")
        print(f"cua metrics: wrote {(args.out / 'metrics.md').as_posix()}", file=sys.stderr)
    print(json.dumps(data, indent=2) if args.json else md)
    return 0


def model_spend(runs: list[Any]) -> dict[str, Any]:
    """Model calls, tokens and estimated cost, by run kind."""
    out: dict[str, Any] = {}
    for kind in ("discovery", "replay"):
        mine = [r for r in runs if r.kind == kind]
        costs = [r.llm.estimated_cost_usd for r in mine]
        unpriced = sum(1 for c in costs if c is None)
        total = sum((c for c in costs if c is not None), Decimal(0))
        out[kind] = {
            "runs": len(mine),
            "llm_calls": sum(r.llm.calls for r in mine),
            "tokens": sum(r.llm.total_tokens for r in mine),
            "estimated_cost_usd": str(total),
            "unpriced_runs": unpriced,
            "llm_wait_s": round(sum(r.llm.wait_s for r in mine), 3),
        }
    return out


def report_markdown(data: dict[str, Any], health: list[Any]) -> str:
    lines = [
        "# Capability metrics",
        "",
        f"{data['runs']} runs read from {', '.join(f'`{r}`' for r in data['roots'])}.",
        "",
        "## Capability health (replays)",
        "",
    ]
    lines += health_table(health) if health else ["No replay on record."]
    lines += [
        "",
        "## Model spend",
        "",
        "| Run kind | Runs | LLM calls | Tokens | Model wait s | Estimated cost |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for kind, s in data["model_spend"].items():
        cost = f"${s['estimated_cost_usd']}" + (
            f" (+{s['unpriced_runs']} unpriced)" if s["unpriced_runs"] else ""
        )
        lines.append(
            f"| {kind} | {s['runs']} | {s['llm_calls']} | {s['tokens']:,} | "
            f"{s['llm_wait_s']:.1f} | {cost} |"
        )
    lines += [
        "",
        "Rates are over the runs counted, which are few for some versions: read them as "
        "observations. Runs with a failure injected into the target app are left out of "
        "health unless `--include-injected` is given; runs refused for want of an approval "
        "are counted (refused) but not rated. Costs come from the price table and "
        "are estimates, not billing data.",
        "",
    ]
    return "\n".join(lines)
