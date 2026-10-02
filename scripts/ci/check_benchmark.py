"""Fail CI on missing smoke coverage, incorrect replay or model use."""

from __future__ import annotations

import argparse
from pathlib import Path

from cua.benchmark.storage import read_runs, read_sessions

TASKS = {"lookup-success", "lookup-unknown-member", "open-without-consent"}


def check(root: Path) -> None:
    sessions = read_sessions(root)
    if len(sessions) != 1 or sessions[0].finished_at is None:
        raise ValueError("expected exactly one completed smoke session")
    session = sessions[0]
    if session.llm != "scripted" or session.strategies != ["inter_cua_replay"]:
        raise ValueError("expected scripted replay-only smoke session")
    rows = read_runs(root)
    expected = {(task, rep) for task in TASKS for rep in (1, 2)}
    if len(rows) != 6 or {(r.task_id, r.repetition) for r in rows} != expected:
        raise ValueError("missing or duplicate smoke invocations")
    if session.runs != len(rows) or set(session.tasks) != TASKS:
        raise ValueError("session coverage disagrees with raw metrics")
    for row in rows:
        if (
            row.session_id != session.session_id
            or row.strategy != "inter_cua_replay"
            or row.match != "exact"
            or row.llm_calls != 0
            or row.duplicate_side_effects != 0
            or row.commits_observed != 0
            or row.cached
            or not row.run_id
        ):
            raise ValueError(f"unsafe or incorrect smoke invocation: {row.task_id}")
    if len({row.run_id for row in rows}) != len(rows):
        raise ValueError("smoke invocations must have independent run IDs")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path)
    args = parser.parse_args()
    try:
        check(args.reports)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"benchmark gate: {exc}\n")
    print("benchmark gate: six independent, correct replays; zero model calls or commits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
