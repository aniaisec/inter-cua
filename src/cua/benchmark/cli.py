"""``cua benchmark list | run | report``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cua.benchmark.models import STRATEGIES, RunMetrics, Strategy, Suite
from cua.benchmark.storage import REPORTS_DIR

EX_USAGE = 64


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    b = sub.add_parser(
        "benchmark",
        help="Compare asking a model every time with discover-once-then-replay",
        description="Run benchmark tasks (bench/tasks/<suite>.yaml) through the repeated-LLM "
        "baseline, discovery and replay, against a mock app started for the session. Every "
        "invocation is appended to bench/reports/runs.jsonl; `report` aggregates them.",
    )
    bsub = b.add_subparsers(dest="benchmark_command", required=True)

    ls = bsub.add_parser("list", help="The tasks in a suite")
    ls.add_argument("--suite", default="core", help="bench/tasks/<suite>.yaml, or a path")
    ls.add_argument("--all", action="store_true", help="Include tasks that need a person")

    run = bsub.add_parser("run", help="Run a benchmark session")
    run.add_argument("--suite", default="core", help="bench/tasks/<suite>.yaml, or a path")
    run.add_argument("--task", action="append", default=[], help="Only this task id (repeatable)")
    run.add_argument("--tag", action="append", default=[], help="Only tasks with this tag")
    run.add_argument(
        "--strategy",
        action="append",
        choices=STRATEGIES,
        default=[],
        help="Only this strategy (repeatable; default: all)",
    )
    run.add_argument("--repetitions", type=int, help="Override every task's repetition count")
    run.add_argument(
        "--discovery-repetitions",
        type=int,
        default=1,
        help="Discovery runs per task (default 1: it is done once)",
    )
    run.add_argument(
        "--llm",
        choices=("scripted", "auto", "anthropic", "gemini"),
        default="scripted",
        help="What the baseline and discovery run on. scripted (default) needs no key and "
        "measures the harness only; a provider spends real money on every baseline run",
    )
    run.add_argument("--model", help="Model id for the chosen provider")
    run.add_argument("--pricing", type=Path, default=Path("bench/pricing.yaml"))
    run.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)

    rep = bsub.add_parser("report", help="Aggregate recorded runs into summary.json / .md")
    rep.add_argument("--session", action="append", default=[], help="Only this session")
    rep.add_argument("--latest", action="store_true", help="Only the most recent session")
    rep.add_argument("--suite", default="core", help="For the per-tag view")
    rep.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    rep.add_argument("--out", type=Path, help="Where to write (default: --reports-dir)")


def main(args: argparse.Namespace) -> int:
    from cua.benchmark.registry import SuiteError, load_suite, select

    try:
        suite = load_suite(args.suite)
    except SuiteError as exc:
        print(f"cua benchmark: {exc}", file=sys.stderr)
        return EX_USAGE

    if args.benchmark_command == "list":
        for t in select(suite, include_manual=args.all):
            flags = [t.risk_level, f"x{t.repetitions}"]
            if t.inject:
                flags.append(f"inject={t.inject}")
            if not t.automated:
                flags.append("needs a person")
            print(f"{t.id:32} {t.truth.kind:17} {' '.join(flags):40} {t.name}")
        return 0

    if args.benchmark_command == "report":
        return _report(args, suite)

    from cua.benchmark.runner import Plan, run_session
    from cua.observability.cost import load_prices

    try:
        tasks = select(suite, task_ids=args.task, tags=args.tag)
    except SuiteError as exc:
        print(f"cua benchmark: {exc}", file=sys.stderr)
        return EX_USAGE
    if not tasks:
        print("cua benchmark: no task matches", file=sys.stderr)
        return EX_USAGE
    strategies: list[Strategy] = list(args.strategy) or list(STRATEGIES)
    if args.llm != "scripted" and any(s != "inter_cua_replay" for s in strategies):
        from cua.agent.llm import NoProviderError, select_client

        try:
            select_client(args.llm, args.model)
        except NoProviderError as exc:
            print(f"cua benchmark: {exc}", file=sys.stderr)
            return EX_USAGE

    plan = Plan(
        tasks=tasks,
        strategies=strategies,
        suite=suite.name,
        llm=args.llm,
        model=args.model,
        repetitions=args.repetitions,
        discovery_repetitions=args.discovery_repetitions,
        prices=load_prices(args.pricing),
        reports_dir=args.reports_dir,
    )
    info = run_session(plan, progress=_progress)
    for note in info.notes:
        print(f"cua benchmark: {note}", file=sys.stderr)
    print(
        f"cua benchmark: session {info.session_id}, {info.runs} runs -> "
        f"{(args.reports_dir / 'runs.jsonl').as_posix()}. "
        f"Summarise with: cua benchmark report --session {info.session_id}",
        file=sys.stderr,
    )
    return 0


def _progress(row: RunMetrics) -> None:
    cost = "" if row.estimated_cost_usd in (None, 0) else f" ${row.estimated_cost_usd}"
    print(
        f"  {row.task_id:32} {row.strategy:20} #{row.repetition:<3} {row.match:9} "
        f"{row.outcome:30} {row.duration_s:6.1f}s llm={row.llm_calls}{cost}",
        file=sys.stderr,
        flush=True,
    )


def _report(args: argparse.Namespace, suite: Suite) -> int:
    from cua.benchmark import report
    from cua.benchmark.storage import read_runs, read_sessions

    sessions = read_sessions(args.reports_dir)
    wanted = list(args.session)
    if args.latest and sessions:
        wanted = [max(sessions, key=lambda s: s.started_at).session_id]
    rows = read_runs(args.reports_dir, wanted)
    if not rows:
        print("cua benchmark: no recorded runs match", file=sys.stderr)
        return EX_USAGE
    summary = report.build(rows, sessions, suite)
    js, md = report.write(summary, args.out or args.reports_dir)
    print(f"cua benchmark: wrote {js.as_posix()} and {md.as_posix()}", file=sys.stderr)
    print(md.read_text(encoding="utf-8"))
    return 0
