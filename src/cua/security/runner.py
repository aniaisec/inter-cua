"""Run the security suite and write its report.

Offline scenarios each run in a lab of their own. Live scenarios share one fresh
mock app, started with the session's canary password; each runs on a worker
thread, as a CLI process would run it (the sync Playwright API allows one per
thread). A probe that raises is a result with ``error`` set, which counts as
not blocked: an attack the harness could not stage is not evidence of a
defence.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

from cua.security.lab import Lab, make_lab, new_canary
from cua.security.models import Metrics, Observed, Report, Result, Scenario, Suite
from cua.security.probes import PROBES

RUNS_ROOT = Path("bench/security/runs")
REPORTS = Path("bench/security/reports")


def run_suite(
    suite: Suite,
    *,
    only: list[str] | None = None,
    live: bool = True,
    runs_root: Path = RUNS_ROOT,
    progress: Callable[[Result], None] | None = None,
) -> Report:
    from cua.benchmark.environment import mockapp

    chosen = [s for s in suite.scenarios if not only or s.id in only]
    unknown = sorted(set(only or []) - {s.id for s in suite.scenarios})
    if unknown:
        raise ValueError(f"no scenario {', '.join(unknown)}")
    session = f"sec_{ULID()}"
    root = runs_root / session
    started = time.monotonic()
    at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    results: list[Result] = []

    def record(result: Result) -> None:
        results.append(result)
        if progress is not None:
            progress(result)

    for sc in (s for s in chosen if not s.live):
        # A lab each: a tampering scenario edits its copy of the capabilities.
        record(_one(make_lab(root / "offline" / sc.id, None), sc))

    live_ones = [s for s in chosen if s.live]
    if live_ones and live:
        canary = new_canary()
        with mockapp(env={"MOCKAPP_OPERATOR_PASSWORD": canary}) as base_url:
            lab = make_lab(root / "live", base_url, canary=canary)
            with ThreadPoolExecutor(max_workers=1) as pool:
                for sc in live_ones:
                    record(pool.submit(_one, lab, sc).result())

    results.sort(key=lambda r: r.scenario)
    by_threat: dict[str, Counter[str]] = {}
    for r in results:
        c = by_threat.setdefault(r.threat, Counter())
        c["attacks"] += 1
        c["blocked"] += int(r.blocked)
    return Report(
        session=session,
        started_at=at,
        duration_s=round(time.monotonic() - started, 1),
        suite=str(len(chosen)) + " scenario(s)" + ("" if live else ", live ones skipped"),
        metrics=Metrics.of(results),
        by_threat={t: dict(c) for t, c in sorted(by_threat.items())},
        results=results,
    )


def _one(lab: Lab, sc: Scenario) -> Result:
    started = time.monotonic()
    error = None
    try:
        observed = PROBES[sc.probe](lab, sc)
    except Exception as exc:  # the probe could not stage its attack
        observed = Observed(verdict=None, detail="the probe failed")
        error = f"{type(exc).__name__}: {exc}"
    return Result(
        scenario=sc.id,
        threat=sc.threat,
        severity=sc.severity,
        title=sc.title,
        live=sc.live,
        expected=sc.expected.verdict,
        observed=observed,
        duration_ms=int((time.monotonic() - started) * 1000),
        error=error,
    )


def write_report(report: Report, out: Path = REPORTS) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    as_json = out / "summary.json"
    data = report.model_dump(mode="json")
    for r, result in zip(data["results"], report.results, strict=True):
        r["blocked"] = result.blocked
    as_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    md = out / "summary.md"
    md.write_text(render(report), encoding="utf-8", newline="\n")
    return md, as_json


def render(report: Report) -> str:
    m = report.metrics
    lines = [
        "# Security benchmark",
        "",
        f"Session `{report.session}`, {report.started_at}, {report.duration_s:.0f} s, "
        f"{report.suite}.",
        "",
        "Every attack is staged against the real components with a scripted model that",
        "follows every instruction a hostile screen gives it; each is judged by its effects",
        "(what reached the attacker's origin, what the app committed or served, where the",
        "canary password turned up), not by what the model decided.",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    for name, value in m.model_dump().items():
        lines.append(f"| `{name}` | {value} |")
    lines += [
        "",
        "| Scenario | Threat | Severity | Expected | Observed | Blocked | What happened |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in report.results:
        what = r.error or r.observed.detail
        failed = [c.name for c in r.observed.checks if not c.passed]
        if failed:
            what += "; failed checks: " + ", ".join(failed)
        lines.append(
            f"| `{r.scenario}` | {r.threat} | {r.severity} | {r.expected} | "
            f"{r.observed.verdict or '-'} | {'yes' if r.blocked else '**NO**'} | "
            f"{what.replace('|', '/')} |"
        )
    return "\n".join(lines) + "\n"
