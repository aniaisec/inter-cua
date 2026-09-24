"""``summary.json`` and ``summary.md``, generated from the raw rows.

Nothing in these files is typed in by hand: re-running the report over the
same ``runs.jsonl`` gives the same numbers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cua.benchmark.aggregation import aggregate
from cua.benchmark.models import RunMetrics, Suite
from cua.benchmark.storage import SessionInfo
from cua.observability.metrics import BUCKETS, run_metrics
from cua.observability.recorder import is_run_dir, read_run

STRATEGY_LABEL = {
    "baseline_llm": "Repeated LLM (baseline)",
    "inter_cua_discovery": "inter-cua discovery (once)",
    "inter_cua_replay": "inter-cua replay",
}


def build(
    rows: list[RunMetrics], sessions: list[SessionInfo], suite: Suite | None
) -> dict[str, Any]:
    tags = {t.id: t.tags for t in suite.tasks} if suite else {}
    summary = aggregate(rows, tags)
    wanted = set(summary["sessions"])
    summary["session_info"] = [
        s.model_dump(mode="json") for s in sessions if s.session_id in wanted
    ]
    summary["scripted"] = any(s.llm == "scripted" for s in sessions if s.session_id in wanted)
    summary["time"] = time_breakdown(rows)
    return summary


def time_breakdown(rows: list[RunMetrics]) -> dict[str, Any]:
    """Mean seconds per run spent in each part of a run (see
    ``cua.observability.metrics``), by strategy, read from the run directories
    still on disk. Run directories are not committed, so a report rebuilt from
    ``runs.jsonl`` alone has no breakdown; everything else is unchanged."""
    out: dict[str, Any] = {}
    for strategy in dict.fromkeys(r.strategy for r in rows):
        totals: dict[str, float] = {}
        read = 0
        for r in rows:
            if r.strategy != strategy or r.cached or not r.run_dir:
                continue
            path = Path(r.run_dir)
            if not is_run_dir(path):
                continue
            for bucket, seconds in run_metrics(read_run(path)).time.items():
                totals[bucket] = totals.get(bucket, 0.0) + seconds
            read += 1
        if read:
            out[strategy] = {
                "runs": read,
                "mean_s": {b: round(totals.get(b, 0.0) / read, 3) for b in BUCKETS},
            }
    return out


def write(summary: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    js = out_dir / "summary.json"
    md = out_dir / "summary.md"
    js.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")
    md.write_text(markdown(summary), encoding="utf-8", newline="\n")
    return js, md


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _ci(value: list[float] | tuple[float, float] | None) -> str:
    return "" if not value else f" ({value[0] * 100:.0f}-{value[1] * 100:.0f})"


def _num(value: Any, fmt: str = "{}") -> str:
    return "-" if value is None else fmt.format(value)


def _usd(value: str | None) -> str:
    return "unpriced" if value is None else f"${value}"


def markdown(summary: dict[str, Any]) -> str:
    lines = ["# Benchmark summary", ""]
    lines += _setup(summary)
    if summary.get("scripted"):
        lines += [
            "> **Scripted model.** At least one session ran the baseline and discovery with "
            "`--llm scripted`: a recorded tool-call sequence played back, not a model. Its "
            "model calls, tokens and cost are zero by construction, and its success rate "
            "measures the harness, not a model. Compare strategies only on a live-model "
            "session.",
            "",
        ]
    lines += ["## By strategy", ""]
    lines += [
        "| Strategy | Runs | Success (95% CI) | Safe stop | Wrong | Escalation | "
        "Human | Median s | P95 s | LLM calls/run | Tokens/run | Cost/run | "
        "Duplicate commits |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, s in summary["strategies"].items():
        lines.append(
            f"| {STRATEGY_LABEL.get(name, name)} | {s['runs']} | "
            f"{_pct(s['success_rate'])}{_ci(s['success_ci95'])} | {_pct(s['safe_stop_rate'])} | "
            f"{_pct(s['wrong_rate'])} | {_pct(s['escalation_rate'])} | "
            f"{_pct(s['human_intervention_rate'])} | {_num(s['latency_median_s'])} | "
            f"{_num(s['latency_p95_s'])} | {_num(s['llm_calls_per_run'])} | "
            f"{_num(s['tokens_per_run'])} | {_usd(s['cost_per_run_usd'])} | "
            f"{s['duplicate_side_effects']} |"
        )
    lines.append("")
    be = summary.get("break_even")
    if be:
        n = be["invocations"]
        lines += [
            "## Model-cost break-even",
            "",
            f"Discovery cost ${be['discovery_usd']} once; the baseline costs "
            f"${be['baseline_per_run_usd']} and replay ${be['replay_per_run_usd']} per "
            "invocation. "
            + (
                f"Discover-then-replay has spent less on the model after **{n}** invocation(s)."
                if n is not None
                else "Replay is not cheaper per run here, so there is no break-even."
            ),
            "Model spend only, from the configured price table; an estimate, not billing.",
            "",
        ]
    if summary["tags"]:
        lines += [
            "## By scenario tag",
            "",
            "| Tag | Strategy | Runs | Success | Safe stop | Wrong |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for t in summary["tags"]:
            lines.append(
                f"| {t['tag']} | {STRATEGY_LABEL.get(t['strategy'], t['strategy'])} | "
                f"{t['runs']} | {_pct(t['success_rate'])} | {_pct(t['safe_stop_rate'])} | "
                f"{_pct(t['wrong_rate'])} |"
            )
        lines.append("")
    if summary.get("time"):
        lines += [
            "## Where the time goes",
            "",
            "Mean seconds per run, split so that the parts add up to the run's wall clock "
            "(`cua metrics run <run_id>` shows one run). *evidence* is the observation and "
            "screenshot stored after each step; *verify* waits for the page to settle and "
            "the checkpoint to hold.",
            "",
            "| Strategy | Runs read | " + " | ".join(BUCKETS) + " | Total |",
            "|---|---:|" + "---:|" * (len(BUCKETS) + 1),
        ]
        for name, t in summary["time"].items():
            means = t["mean_s"]
            lines.append(
                f"| {STRATEGY_LABEL.get(name, name)} | {t['runs']} | "
                + " | ".join(f"{means[b]:.2f}" for b in BUCKETS)
                + f" | {sum(means.values()):.2f} |"
            )
        lines.append("")
    lines += [
        "## By task",
        "",
        "| Task | Strategy | Runs | Success | Safe stop | Wrong | Median s | P95 s | "
        "LLM calls | Tokens/run | Cost/run | Outcomes |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for t in summary["tasks"]:
        outcomes = ", ".join(f"{k} x{v}" for k, v in t["outcomes"].items())
        lines.append(
            f"| {t['task']} | {STRATEGY_LABEL.get(t['strategy'], t['strategy'])} | {t['runs']} | "
            f"{_pct(t['success_rate'])} | {_pct(t['safe_stop_rate'])} | {_pct(t['wrong_rate'])} | "
            f"{_num(t['latency_median_s'])} | {_num(t['latency_p95_s'])} | {t['llm_calls']} | "
            f"{_num(t['tokens_per_run'])} | {_usd(t['cost_per_run_usd'])} | {outcomes} |"
        )
    lines += [
        "",
        "## Reading this",
        "",
        "- **Success**: the correct answer was delivered — the right outputs or the right "
        "business outcome — with the right side effects. Each task states its ground truth "
        "independently of either strategy (`bench/tasks/`).",
        "- **Safe stop**: no answer, and nothing wrong done: the run failed or escalated "
        "cleanly. Work a person picks up, not a mistake.",
        "- **Wrong**: a wrong answer, or a commit that should not have happened (without "
        "consent, or a second time for the same request).",
        "- The baseline has no typed channel for a business outcome; a stop whose stated "
        "reason matches the task's declared pattern counts as the right answer. This judge "
        "is a heuristic and is declared per task.",
        "- Costs come from `bench/pricing.yaml` and are estimates. Replay makes no model "
        "call, so its model cost is zero; its cost is browser time, shown as latency.",
        "",
    ]
    return "\n".join(lines)


def _setup(summary: dict[str, Any]) -> list[str]:
    lines = ["## Setup", ""]
    for s in summary["session_info"]:
        lines.append(
            f"- `{s['session_id']}` ({s['started_at']}): suite `{s['suite']}`, "
            f"{s['runs']} runs, model `{s['llm']}`"
            + (f" / `{s['model']}`" if s.get("model") else "")
            + f"; commit `{(s.get('git_commit') or '?')[:7]}`"
            + (" (dirty)" if s.get("git_dirty") else "")
            + f"; Python {s['python']}, Playwright {s.get('playwright') or '?'}, "
            f"Chromium {s.get('browser') or '?'}; {s['platform']}"
        )
    models = sorted({m for t in summary["tasks"] for m in t["models"]})
    if models:
        lines.append(f"- Models that answered: {', '.join(models)}")
    lines.append("")
    return lines
