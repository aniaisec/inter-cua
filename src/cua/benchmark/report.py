"""``summary.json`` and ``summary.md``, generated from the raw rows.

Nothing in these files is typed in by hand: re-running the report over the
same ``runs.jsonl`` gives the same numbers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cua.benchmark.aggregation import aggregate
from cua.benchmark.inference import compare
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
    lost = [r for r in rows if lost_to_provider(r)]
    rows = [r for r in rows if not lost_to_provider(r)]
    summary = aggregate(rows, tags)
    summary["lost_runs"] = _lost(lost)
    wanted = set(summary["sessions"])
    summary["session_info"] = [
        s.model_dump(mode="json") for s in sessions if s.session_id in wanted
    ]
    summary["scripted"] = any(s.llm == "scripted" for s in sessions if s.session_id in wanted)
    summary["time"] = time_breakdown(rows)
    summary["comparison"] = compare(rows, tags)
    return summary


def lost_to_provider(row: RunMetrics) -> bool:
    """The model call failed before the model answered once: a quota, a
    spending cap, a key the provider refused. Nothing about either strategy
    was measured, so the run is set aside and counted, not scored. A model
    error after the model has taken part stays in: that is the baseline's
    own reliability."""
    return row.error_code == "LLM_ERROR" and row.llm_calls == 0


def _lost(rows: list[RunMetrics]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], int] = {}
    for r in rows:
        key = (r.session_id, r.task_id, r.strategy)
        groups[key] = groups.get(key, 0) + 1
    return [
        {"session": s, "task": t, "strategy": st, "runs": n}
        for (s, t, st), n in sorted(groups.items())
    ]


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
    if summary.get("lost_runs"):
        lines += [
            "> **Runs lost to the provider, left out of every number below.** The model "
            "call failed before the model answered once (a quota, a spending cap, a "
            "refused key), so nothing was measured: "
            + "; ".join(
                f"{g['task']} / {STRATEGY_LABEL.get(g['strategy'], g['strategy'])} x{g['runs']} "
                f"(`{g['session']}`)"
                for g in summary["lost_runs"]
            )
            + ".",
            "",
        ]
    if summary.get("comparison"):
        lines += _comparison(summary["comparison"])
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
    if summary.get("categories"):
        lines += [
            "## By category",
            "",
            "Forbidden: downloads, uploads and requests to the attacker's origin the app "
            "recorded during the runs (any one scores its run wrong).",
            "",
            "| Category | Strategy | Runs | Success | Safe stop | Wrong | Median s | "
            "Duplicate commits | Forbidden |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for c in summary["categories"]:
            lines.append(
                f"| {c['category']} | {STRATEGY_LABEL.get(c['strategy'], c['strategy'])} | "
                f"{c['runs']} | {_pct(c['success_rate'])} | {_pct(c['safe_stop_rate'])} | "
                f"{_pct(c['wrong_rate'])} | {_num(c['latency_median_s'])} | "
                f"{c['duplicate_side_effects']} | {c['forbidden_effects']} |"
            )
        lines.append("")
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
            + (f" ({s['machine']})" if s.get("machine") else "")
        )
        lines += _configuration(s)
    versions: dict[str, set[str]] = {}
    tasks: dict[str, set[str]] = {}
    for s in summary["session_info"]:
        for task, v in s.get("capability_versions", {}).items():
            versions.setdefault(task, set()).add(v)
        for task, v in s.get("task_versions", {}).items():
            tasks.setdefault(task, set()).add(v)
    if versions:
        caps = sorted({v for vs in versions.values() for v in vs})
        lines.append(f"- Replayed: {'; '.join(f'`{c}`' for c in caps)}")
    for what, seen in (("capability", versions), ("task definition", tasks)):
        changed = sorted(t for t, vs in seen.items() if len(vs) > 1)
        if changed:
            lines.append(
                f"- **Not comparable across sessions**: the {what} changed between "
                f"sessions for {', '.join(changed)}"
            )
    models = sorted({m for t in summary["tasks"] for m in t["models"]})
    if models:
        lines.append(f"- Models that answered: {', '.join(models)}")
    lines.append("")
    return lines


QUESTION_FORMAT = {
    "llm_calls": "calls",
    "cost": "usd",
    "latency": "s",
    "repeatability": "rate",
    "drift": "rate",
    "humans": "rate",
    "safety": "rate",
}


def _fmt(kind: str, value: float | None, signed: bool = False) -> str:
    if value is None:
        return "-"
    sign = "+" if signed and value > 0 else ""
    if kind == "rate":
        return f"{sign}{value * 100:.1f}%"
    if kind == "usd":
        return f"{sign}${value:.4f}" if value >= 0 else f"-${-value:.4f}"
    if kind == "s":
        return f"{sign}{value:.2f} s"
    return f"{sign}{value:.2f}"


def _interval(kind: str, ci: list[float] | tuple[float, float] | None) -> str:
    if not ci:
        return ""
    lo, hi = ci
    if kind == "rate":
        return f" ({lo * 100:.1f} to {hi * 100:.1f})"
    return f" ({_fmt(kind, lo)} to {_fmt(kind, hi)})"


def _side(kind: str, side: dict[str, Any]) -> str:
    if not side.get("n"):
        return "-"
    return f"{_fmt(kind, side['value'])}{_interval(kind, side.get('ci95'))}, n={side['n']}"


def _comparison(c: dict[str, Any]) -> list[str]:
    lines = ["## What the measurements support", ""]
    fit = c.get("adequacy", {})
    runs = "; ".join(
        f"{STRATEGY_LABEL.get(s, s)}: {_runs_per_task(a)} per task (protocol {a['required']}+)"
        for s, a in fit.items()
    )
    model = ", ".join(f"`{m}`" for m in c.get("models", [])) or "none"
    lines += [
        f"Baseline vs replay on the {len(c['tasks'])} task(s) both ran"
        + (
            f" (left out, run by one strategy only: {', '.join(c['excluded_tasks'])})"
            if c["excluded_tasks"]
            else ""
        )
        + f". Baseline model: {model}. {runs}.",
        "",
        f"**Strength of every finding below: {c['strength']}.**",
        "",
        "| # | Question | Measure | Repeated LLM (95% CI) | inter-cua replay (95% CI) | "
        "Replay - LLM (95% CI) | Finding |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, q in enumerate(c["questions"], 1):
        kind = QUESTION_FORMAT.get(q["id"], "calls")
        diff = q["difference"]
        lines.append(
            f"| {i} | {q['question']} | {q['metric']} | {_side(kind, q['baseline'])} | "
            f"{_side(kind, q['replay'])} | {_fmt(kind, diff['estimate'], signed=True)}"
            f"{_interval(kind, diff['ci95'])} | {q['verdict']} |"
        )
    lines += ["", *_details(c["questions"]), ""]
    lines += [
        "A finding is stated only where the 95% interval of the difference excludes zero; "
        '"no detectable difference" means these runs cannot tell, not that there is none. '
        "Rates: Wilson intervals, and Newcombe's for a difference of two. Means and medians: "
        f"percentile bootstrap, {c['bootstrap']['resamples']} resamples, seed "
        f"{c['bootstrap']['seed']}. A synthetic suite on one mock app: these numbers say "
        "nothing about other applications.",
        "",
    ]
    return lines


def _runs_per_task(a: dict[str, Any]) -> str:
    low, mid = a["min_runs_per_task"], a["median_runs_per_task"]
    return f"{low} runs" if low == mid else f"at least {low} runs (median {mid})"


def _details(questions: list[dict[str, Any]]) -> list[str]:
    by_id = {q["id"]: q for q in questions}
    out: list[str] = []
    lat = by_id.get("latency")
    if lat:
        out.append(
            "- Latency: "
            + "; ".join(
                f"{side} mean {_num(lat[side].get('mean'))} s, stdev "
                f"{_num(lat[side].get('stdev'))} s, p95 {_num(lat[side].get('p95'))} s"
                for side in ("baseline", "replay")
                if lat[side].get("n")
            )
            + "."
        )
    rep = by_id.get("repeatability")
    if rep:
        parts = []
        for side in ("baseline", "replay"):
            cons = rep[side].get("consistency") or {}
            if cons.get("tasks"):
                parts.append(
                    f"{side}: {_pct(rep[side]['exact']['value'])} exact; the same outcome on "
                    "every repetition in "
                    f"{cons['same_outcome_every_time']} of {cons['tasks']} tasks, median "
                    f"latency CV {_num(cons.get('latency_cv_median'))}"
                )
        if parts:
            out.append("- Repeatability: " + "; ".join(parts) + ".")
    drift = by_id.get("drift")
    if drift:
        parts = [
            f"{side} {_pct(drift[side]['exact']['value'])} exact, "
            f"{_pct(drift[side]['safe_stop']['value'])} safe stop, "
            f"{_pct(drift[side]['value'])} wrong (n={drift[side]['n']})"
            for side in ("baseline", "replay")
            if drift[side].get("n")
        ]
        if parts:
            out.append("- Under drift: " + "; ".join(parts) + ".")
    safety = by_id.get("safety")
    if safety:
        parts = [
            f"{side} {safety[side]['wrong']['count']} wrong, "
            f"{safety[side]['duplicate_commits']} duplicate and "
            f"{safety[side]['unexpected_commits']} unconsented commit(s), "
            f"{safety[side]['forbidden_effects']} forbidden effect(s), "
            f"{safety[side]['policy_blocks']} policy block(s)"
            for side in ("baseline", "replay")
            if safety[side].get("n")
        ]
        if parts:
            out.append("- Safety: " + "; ".join(parts) + ".")
    return out


def _configuration(s: dict[str, Any]) -> list[str]:
    """What else a session recorded about how it ran: enough to rerun it, or
    to see that two sessions did not run the same thing."""
    out: list[str] = []
    if s.get("llm_settings"):
        out.append(
            "  - model settings: " + "; ".join(f"{k} {v}" for k, v in s["llm_settings"].items())
        )
    marks = [
        f"{name} `{s[key]}`"
        for name, key in (("prompt", "prompt_version"), ("app", "app_version"))
        if s.get(key)
    ]
    if s.get("task_versions"):
        marks.append(f"{len(s['task_versions'])} task fingerprints")
    if marks:
        out.append("  - versions: " + ", ".join(marks))
    reps = dict(s.get("repetitions_by_strategy") or {})
    if reps or s.get("repetitions"):
        spelled = [f"{STRATEGY_LABEL.get(k, k)} x{v}" for k, v in reps.items()]
        if s.get("repetitions"):
            spelled.append(f"otherwise x{s['repetitions']}")
        out.append("  - repetitions: " + ", ".join(spelled))
    return out
