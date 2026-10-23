"""Conservative local retention. Retry authority and review evidence are permanent.

Only screenshots of terminal, durably owned replay runs are eligible. Legacy,
discovery and handoff runs need further provenance/session migration first.
"""

from __future__ import annotations

import argparse
import json
import math
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cua.observability.correlation import read_header
from cua.persistence import _read_jsonl, append_jsonl, read_json
from cua.replay.ownership import InvocationBusy, SQLiteInvocationStore, execution_lock
from cua.replay.result import RESULT


def safe_path(root: Path, path: Path) -> bool:
    """Reject traversal, symlinks and Windows junctions, including ancestors."""
    try:
        path.relative_to(root)
        for part in (path, *path.parents):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return False
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def inspect(
    root: Path, tenant: str, *, retention_days: float = 30, apply: bool = False
) -> dict[str, Any]:
    """Build a scoped plan, rechecking ownership under its execution lock on apply.

    Run with trusted local writers stopped. No process is killed: legacy session
    metadata cannot prove process identity, and a PID is never sufficient.
    """
    if not math.isfinite(retention_days) or retention_days < 0:
        raise ValueError("retention days must be finite and nonnegative")
    root = root.absolute()
    report: dict[str, Any] = {
        "tenant": tenant,
        "dry_run": not apply,
        "candidates": [],
        "deleted": 0,
        "bytes_eligible": 0,
        "storage_bytes": 0,
        "cleanup_failures": 0,
        "unknown_side_effects": 0,
        "escalated": 0,
        "expired_sessions": 0,
        "preserved_runs": 0,
    }
    if not root.exists():
        return report
    if not safe_path(root, root):
        raise ValueError("runs directory must not contain symlinks or junctions")
    audit = root / ".maintenance.jsonl"
    for target in (audit, audit.with_name(audit.name + ".lock")):
        if (target.exists() or target.is_symlink()) and not safe_path(root, target):
            raise ValueError("unsafe maintenance audit path")
    report["previous_cleanup_failures"] = cleanup_failures(root, tenant)
    authority = root / ".invocations"
    # Inspection must not initialize an empty authority or create lock files.
    if not authority.exists():
        report["preserved_runs"] = len(list(root.glob("run_*")))
        return report
    for name in ("format.json", "invocations.sqlite3", "owners"):
        path = authority / name
        if path.exists() and not safe_path(root, path):
            raise ValueError("unsafe invocation authority path")
    if read_json(authority / "format.json") != {"version": 1}:
        raise ValueError("unsupported invocation authority")
    store = SQLiteInvocationStore.existing(root)
    now = datetime.now(UTC)
    for record in store.records():
        if record.tenant != tenant:
            continue
        run = root / record.run_id
        if not safe_path(root, run):
            report["preserved_runs"] += 1
            continue
        header_path = run / "run.json"
        if not safe_path(root, header_path):
            report["preserved_runs"] += 1
            continue
        header = read_header(run)
        if (
            header.kind != "replay"
            or header.correlation.tenant_id != tenant
            or header.correlation.run_id != record.run_id
        ):
            report["preserved_runs"] += 1
            continue
        for path in run.rglob("*"):
            if safe_path(root, path) and path.is_file():
                report["storage_bytes"] += path.stat().st_size
        report["unknown_side_effects"] += record.side_effect == "unknown"
        report["escalated"] += record.phase == "escalated"
        for request in (run / "interventions").glob("*.json"):
            if safe_path(root, request):
                data = read_json(request)
                if record.phase == "escalated" and datetime.fromisoformat(data["expires_at"]) < now:
                    report["expired_sessions"] += 1
        result_path = run / "result.json"
        if (
            record.phase != "finished"
            or record.side_effect == "unknown"
            or (run / "handoff_state.json").exists()
            or (run / "handoff_state.json").is_symlink()
            or not safe_path(root, result_path)
        ):
            report["preserved_runs"] += 1
            continue
        answer = RESULT.validate_python(read_json(result_path))
        if (
            answer.model_dump(mode="json") != record.result
            or answer.side_effect != record.side_effect
        ):
            raise ValueError("run result differs from invocation authority")
        if (now.timestamp() - result_path.stat().st_mtime) / 86400 < retention_days:
            continue
        shots = run / "screenshots"
        if not shots.exists() or not safe_path(root, shots):
            continue
        for path in sorted(shots.glob("*.png")):
            if not safe_path(root, path) or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            report["candidates"].append(relative)
            report["bytes_eligible"] += path.stat().st_size
            if apply:
                try:
                    owner_lock = authority / "owners" / f"{record.key_hash}.lock"
                    if (owner_lock.exists() or owner_lock.is_symlink()) and not safe_path(
                        root, owner_lock
                    ):
                        raise ValueError("unsafe execution lock")
                    with execution_lock(root, record.key_hash):
                        if store.get(record.key_hash) != record or not safe_path(root, path):
                            raise ValueError("cleanup candidate changed")
                        # Persist intent before deleting. A failure leaves retry safety intact.
                        append_jsonl(
                            root / ".maintenance.jsonl",
                            {
                                "event": "cleanup.intent",
                                "tenant": tenant,
                                "path": relative,
                                "at": now.isoformat(),
                            },
                        )
                        path.unlink()
                        report["deleted"] += 1
                except (OSError, ValueError, TimeoutError, InvocationBusy):
                    report["cleanup_failures"] += 1
    if apply:
        append_jsonl(
            root / ".maintenance.jsonl",
            {
                "event": "cleanup.completed",
                "tenant": tenant,
                "at": now.isoformat(),
                "deleted": report["deleted"],
                "failures": report["cleanup_failures"],
            },
        )
    return report


def cleanup_failures(root: Path, tenant: str) -> int:
    return sum(
        int(row["failures"])
        for row in _read_jsonl(root / ".maintenance.jsonl")
        if row.get("event") == "cleanup.completed" and row.get("tenant") == tenant
    )


def add_parser(sub: Any) -> None:
    parser = sub.add_parser("maintenance", help="Inspect retention or clean completed screenshots")
    commands = parser.add_subparsers(dest="maintenance_command", required=True)
    for command in ("inspect", "cleanup"):
        child = commands.add_parser(command)
        child.add_argument("--runs-dir", type=Path, default=Path("evidence/runs"))
        child.add_argument("--tenant", default="local")
        child.add_argument("--retention-days", type=float, default=30)
        if command == "cleanup":
            child.add_argument("--apply", action="store_true", help="Apply the scoped plan")


def main(args: argparse.Namespace) -> int:
    try:
        report = inspect(
            args.runs_dir,
            args.tenant,
            retention_days=args.retention_days,
            apply=getattr(args, "apply", False),
        )
        print(json.dumps(report, indent=2))
        return 1 if report["cleanup_failures"] else 0
    except (OSError, ValueError) as exc:
        print(f"cua maintenance: {exc}", file=sys.stderr)
        return 1
