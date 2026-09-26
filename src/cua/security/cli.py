"""``cua security list | run``.

``run`` stages every scenario in ``bench/security/scenarios.yaml`` (a fresh
mock app of its own for the live ones; ``--offline`` skips them), prints one
line per attack, and writes ``bench/security/reports/summary.{md,json}``.
Exit 0 when every attack was blocked, 1 otherwise. Imports no model client
until a live discovery scenario runs, and that one's model is a script.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cua.termlink import link

EX_USAGE = 64


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    s = sub.add_parser(
        "security",
        help="The security benchmark: hostile screens, tampered artifacts, replayed consent",
        description="Stage each attack in bench/security/scenarios.yaml against the real "
        "components, with a scripted model that follows every injected instruction, and "
        "count what it achieved.",
    )
    ssub = s.add_subparsers(dest="security_command", required=True)
    ls = ssub.add_parser("list", help="The scenarios, by threat")
    ls.add_argument("--suite", type=Path, default=Path("bench/security/scenarios.yaml"))
    r = ssub.add_parser("run", help="Run the scenarios and write the report")
    r.add_argument("--suite", type=Path, default=Path("bench/security/scenarios.yaml"))
    r.add_argument("--scenario", action="append", default=[], help="Only these (repeatable)")
    r.add_argument("--offline", action="store_true", help="Skip the scenarios that need a browser")
    r.add_argument("--out", type=Path, default=Path("bench/security/reports"))
    r.add_argument("--runs-root", type=Path, default=Path("bench/security/runs"))


def main(args: argparse.Namespace) -> int:
    from pydantic import ValidationError

    from cua.security.models import load_suite

    try:
        suite = load_suite(args.suite)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"cua security: {exc}", file=sys.stderr)
        return EX_USAGE
    if args.security_command == "list":
        for sc in suite.scenarios:
            where = "live" if sc.live else "offline"
            print(f"{sc.id}  [{sc.threat}, {sc.severity}, {where}]  expect {sc.expected.verdict}")
            print(f"    {sc.title}")
        return 0

    from cua.security.models import Result
    from cua.security.runner import run_suite, write_report

    def progress(r: Result) -> None:
        mark = "blocked" if r.blocked else "NOT BLOCKED"
        print(f"{mark:12} {r.scenario}: {r.error or r.observed.detail}", file=sys.stderr)

    try:
        report = run_suite(
            suite,
            only=args.scenario or None,
            live=not args.offline,
            runs_root=args.runs_root,
            progress=progress,
        )
    except ValueError as exc:
        print(f"cua security run: {exc}", file=sys.stderr)
        return EX_USAGE
    md, _ = write_report(report, args.out)
    m = report.metrics
    print(
        f"{m.blocked_count}/{m.attack_count} attacks blocked; unsafe actions "
        f"{m.unsafe_action_count}, secret exposures {m.secret_exposure_count}, policy "
        f"bypasses {m.policy_bypass_count}, approval bypasses {m.approval_bypass_count}, "
        f"tenant isolation failures {m.tenant_isolation_failures}"
    )
    print(f"report: {link(md, stream=sys.stdout)}")
    return 0 if m.blocked_count == m.attack_count else 1
