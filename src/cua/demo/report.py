"""Regenerate a demo report from the manifest and the actual run files."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

STAGES = (
    "Discovery",
    "Approval",
    "Repeated replay",
    "Drift",
    "Handoff/resume",
    "Risky side effect",
    "Security",
)


def within(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("evidence path escapes demo directory")
    return path


def validate_transcript(root: Path, manifest: dict[str, Any]) -> None:
    calls = {c["label"]: c for c in manifest["calls"]}
    expected = {
        "discovery": (0, "done", None),
        "draft-refused": (1, "failure", "POLICY_BLOCKED"),
        "describe": (0, None, None),
        "approve": (0, None, None),
        "drift": (3, "escalated", None),
        "resume": (0, "success", None),
        "no-consent": (1, "failure", "POLICY_BLOCKED"),
        "valid-consent": (0, "success", None),
        "reused-consent": (1, "failure", "POLICY_BLOCKED"),
        "security": (0, None, None),
    }
    for label, (code, kind, error) in expected.items():
        call = calls[label]
        result = call["result"]
        if call["exit_code"] != code or (kind and result["kind"] != kind):
            raise ValueError(f"unexpected outcome for {label}")
        if error and result["code"] != error:
            raise ValueError(f"unexpected error for {label}")
    drift = calls["drift"]["result"]
    if drift["reason"] != "STUCK" or drift["step_id"] != "search.submit":
        raise ValueError("drift did not escalate at the renamed button")
    resumed = calls["resume"]["result"]
    handoff = resumed["handoffs"][0]
    if handoff["resumed_after_checkpoint"] != "cp.done":
        raise ValueError("the handoff did not resume at the completed lookup")
    if handoff["decided_by"] != "evidence-bot" or handoff["human_actions_count"] < 1:
        raise ValueError("the scripted operator did not act")
    run = within(root, resumed["run_dir"])
    recorded = json.loads((run / "result.json").read_text(encoding="utf-8"))
    if recorded["kind"] != "success" or recorded["handoffs"] != resumed["handoffs"]:
        raise ValueError("handoff transcript does not match run evidence")
    for label, effect in (
        ("no-consent", "none"),
        ("valid-consent", "committed"),
        ("reused-consent", "none"),
    ):
        if calls[label]["result"]["side_effect"] != effect:
            raise ValueError("consent transcript contains an unexpected side effect")
    if manifest["commits"] != 1:
        raise ValueError("demo did not commit exactly once")
    draft = json.loads((root / "draft.json").read_text(encoding="utf-8"))
    approved = json.loads((root / "approved.json").read_text(encoding="utf-8"))
    if draft["approval_state"] != "draft" or approved["approval_state"] != "approved":
        raise ValueError("draft and approved snapshots are required")
    for key in ("id", "name", "version", "steps", "contract"):
        if draft[key] != approved[key]:
            raise ValueError("approval changed the demonstrated capability")


def generate(root: Path) -> tuple[Path, Path]:
    manifest: dict[str, Any] = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_version"] != 1 or manifest["status"] != "complete":
        raise ValueError("only a complete version-1 demo can be reported")
    stages = manifest["stages"]
    if tuple(s["name"] for s in stages) != STAGES or not all(s["passed"] for s in stages):
        raise ValueError("all seven demo stages must pass")
    validate_transcript(root, manifest)
    runs = manifest["replay_runs"]
    if len(runs) != manifest["repetitions"] or len(set(runs)) != len(runs) or not runs:
        raise ValueError("replay evidence must contain the requested independent invocations")
    replay_calls = 0
    identities: set[str] = set()
    for relative in runs:
        run = within(root, relative)
        header = json.loads((run / "run.json").read_text(encoding="utf-8"))
        result = json.loads((run / "result.json").read_text(encoding="utf-8"))
        if header["run_id"] in identities or header["request"]["idempotency_key"] is not None:
            raise ValueError("replay evidence is duplicated or uses the idempotency cache")
        identities.add(header["run_id"])
        if header["kind"] != "replay" or result["kind"] != "success":
            raise ValueError("replay evidence is missing or unsuccessful")
        if result["outputs"]["savings_balance"] != "1411.21" or result["side_effect"] != "none":
            raise ValueError("replay returned an unexpected business result")
        calls = run / "model_calls.jsonl"
        if calls.exists():
            replay_calls += sum(
                bool(line.strip()) for line in calls.read_text(encoding="utf-8").splitlines()
            )
    if replay_calls:
        raise ValueError("model calls were found during replay")
    security = json.loads(within(root, manifest["security_report"]).read_text(encoding="utf-8"))
    metrics = security["metrics"]
    if metrics["attack_count"] != 1 or metrics["blocked_count"] != 1:
        raise ValueError("security evidence did not contain the expected blocked attack")
    if any(
        metrics[k]
        for k in (
            "unsafe_action_count",
            "secret_exposure_count",
            "policy_bypass_count",
            "approval_bypass_count",
            "tenant_isolation_failures",
        )
    ):
        raise ValueError("security evidence contains an unsafe effect")
    summary = {
        "started_at": manifest["started_at"],
        "discovery_provider": manifest["discovery_provider"],
        "operator": manifest["operator"],
        "replay_invocations": len(runs),
        "replay_model_calls": replay_calls,
        "commits": manifest["commits"],
        "stages": stages,
        "security": metrics,
    }
    lines = [
        "# inter-cua reproducible demo",
        "",
        f"Started: {summary['started_at']}. Discovery: `{summary['discovery_provider']}`.",
        f"Operator: {summary['operator']}. Review and consent: scripted demo-reviewer.",
        "",
        "Scripted discovery validates the mechanism, not live-model planning quality.",
        "The scripted operator exercises the real console and the same browser session;",
        "its timing is not a measurement of a person. This is an evidence demo, not a benchmark.",
        "",
        "| Stage | Result | Evidence |",
        "|---|---|---|",
    ]
    for stage in stages:
        lines.append(f"| {stage['name']} | passed | {stage['detail']} |")
    lines += [
        "",
        f"Independent successful replays: **{len(runs)}**; "
        f"model calls in replay: **{replay_calls}**.",
        f"Risky commits: **{summary['commits']}**; hostile-page attacks blocked: **1/1**.",
        "",
        "## Replay evidence",
        "",
        "Each directory contains its run header, result, log and masked observations.",
        "",
    ]
    lines += [f"- [{Path(run).name}]({run}/result.json)" for run in runs]
    lines += [
        "",
        "[Command transcript (tokens redacted)](manifest.json)",
        "[Security report](workspace/security-report/summary.md)",
        "",
        "The workspace contains local runtime state, including review receipts and handoff",
        "plumbing. Inspect and redact selected evidence before sharing the workspace.",
        "",
    ]
    md, js = root / "summary.md", root / "summary.json"
    md.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    js.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")
    return md, js


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    args = parser.parse_args(argv)
    try:
        md, _ = generate(args.session)
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        print(f"demo report: {type(exc).__name__}: incomplete or invalid demo evidence")
        return 1
    print(md)
    return 0
