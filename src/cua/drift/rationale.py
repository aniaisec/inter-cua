"""``rationale.md``: the candidate record, written for the person who decides
on it. Regenerated from ``candidate.json`` whenever the record changes, so the
two never disagree."""

from __future__ import annotations

import json
from typing import Any

from cua.drift.models import CandidateRecord, Check, SideResult


def render(r: CandidateRecord, here: str) -> str:
    """``here``: the candidate's directory, as the commands in it should name it."""
    event = r.drift[0]
    lines = [
        f"# {r.name} v{r.version}: candidate repair of v{r.base.version}",
        "",
        f"Proposed {r.created_at} from run `{event.run_id}`. It is a draft: nothing calls it, "
        f"and v{r.base.version} ({r.base.status}) keeps running until a person approves this.",
        "",
        "## What drifted",
        "",
        f"- Step `{event.step_id}` of {r.name} v{event.version}, tenant `{event.tenant_id}` "
        f"({event.app_family}), at {event.at}.",
        f"- **{event.kind}**: {event.reason}.",
        f"- Rungs on that run: {', '.join(event.observed_rungs) or '-'} "
        f"(recorded on `{event.expected_locator_rung}`).",
    ]
    if event.injected:
        lines.append(
            f"- The run had a fault injected into the app on purpose (`{event.injected}`): "
            "this drift was produced, not found."
        )
    lines += [
        f"- Classified from: {event.basis}.",
        "",
        "## The proposed change",
        "",
        f"Only the ladder of `{r.change.step_id}` changes. New rungs go in front; the recorded "
        "ones stay behind them, so a screen that still shows the old control is still served.",
        "",
        "Before:",
        "",
        *_ladder(r.change.before),
        "",
        "After:",
        "",
        *_ladder(r.change.after, added=len(r.change.added)),
        "",
        "## Why it is the same control",
        "",
        f"- {r.control.found_by}: a {r.control.role} named {r.control.name!r}"
        + (f" (recorded as {r.control.recorded_name!r})" if r.control.recorded_name else "")
        + f", in the `{r.control.frame or 'top'}` frame.",
    ]
    if r.control.corroborated_by:
        lines.append(
            f"- A person, {r.control.corroborated_by}, clicked a {r.control.role} named "
            f"{r.control.name!r} when that run was handed over."
        )
    lines += [
        "",
        "## Evidence",
        "",
        *[f"- [{p}]({p})" for p in r.evidence],
        "",
        "## Security and safety checks",
        "",
        *_checks(r.checks),
        "",
        "## Benchmark",
        "",
    ]
    ev = r.evaluation
    if ev is None or ev.artifact_hash != r.artifact_hash:
        lines += [
            "Not evaluated yet. Replay it beside the version it repairs, on the benchmark "
            "tasks for this capability (needs Chromium; starts its own mock app):",
            "",
            f"    cua drift evaluate {r.name} --version {r.version}",
        ]
    else:
        lines += [
            f"Evaluated {ev.at}: suite `{ev.suite}`, {ev.repetitions} repetition(s) per task "
            f"and version, runs in `{ev.runs_dir}`."
            + (
                f" Only the tasks asked for ran: {', '.join(ev.task_filter)}."
                if ev.task_filter
                else ""
            ),
            "",
            f"| task | inject | v{r.base.version} (incumbent) | v{r.version} (candidate) |",
            "|---|---|---|---|",
        ]
        for t in ev.tasks:
            mark = " (the drift)" if t.reproduces_drift else ""
            lines.append(
                f"| {t.task_id}{mark} | {t.inject or '-'} | {_side(t.incumbent)} | "
                f"{_side(t.candidate)} |"
            )
        lines += ["", "Gates:", ""]
        lines += [f"- {'PASS' if g.passed else 'FAIL'} `{g.id}`: {g.detail}" for g in ev.gates]
        lines += ["", f"**Evaluation {'passed' if ev.passed else 'failed'}.**"]
    lines += ["", "## Decision", ""]
    if r.rejection is not None:
        lines.append(f"Rejected by {r.rejection.by} at {r.rejection.at}: {r.rejection.reason}")
    else:
        lines += [
            "To approve, read it and approve exactly what you read (refused until the "
            "evaluation above has passed on this content):",
            "",
            f"    cua describe {here}/capability.json",
            f"    cua approve {here}/capability.json --by <your name>",
            "",
            "To turn it down:",
            "",
            f'    cua drift reject {r.name} --version {r.version} --by <your name> --reason "..."',
        ]
    return "\n".join(lines) + "\n"


def _ladder(rungs: list[dict[str, Any]], *, added: int = 0) -> list[str]:
    return [
        f"{i + 1}. `{json.dumps(rung, sort_keys=True)}`" + ("  (new)" if i < added else "")
        for i, rung in enumerate(rungs)
    ]


def _checks(checks: list[Check]) -> list[str]:
    return [f"- {c.result.upper()} `{c.id}`: {c.detail}" for c in checks]


def _side(s: SideResult) -> str:
    return (
        f"{s.exact}/{s.runs} exact"
        + (f", {s.wrong} wrong" if s.wrong else "")
        + (f" ({', '.join(sorted(set(s.outcomes)))})" if s.exact < s.runs else "")
    )
