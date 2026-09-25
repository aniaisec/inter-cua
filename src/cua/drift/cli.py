"""``cua drift scan | report | propose | evaluate | candidates | show | reject``.

``scan`` and ``report`` read run directories and write nothing. ``propose``
writes a candidate under ``capabilities/candidates/`` and nothing else;
``evaluate`` replays it (a fresh mock app, Chromium) and records the result
in the candidate; ``reject`` records a decision in it. Approving a candidate
is ``cua describe`` and ``cua approve``, as for any capability. Imports no
model client.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cua.termlink import link

EX_USAGE = 64


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    d = sub.add_parser(
        "drift",
        help="Where the app has drifted from its capabilities, and candidate repairs",
        description="Drift is the app no longer matching what a capability recorded: a "
        "control renamed, moved, duplicated or gone; a checkpoint or output that no longer "
        "holds. It is read from run directories. A drift that broke a run can lead to a "
        "candidate repair, which is evaluated and then approved by a person like any "
        "version; nothing is changed in production until then.",
    )
    dsub = d.add_subparsers(dest="drift_command", required=True)

    def runs(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--runs-dir",
            type=Path,
            action="append",
            default=[],
            help="Where to look for runs (repeatable; default: evidence/ and bench/runs/)",
        )

    def caps(p: argparse.ArgumentParser) -> None:
        p.add_argument("--capabilities-dir", type=Path, default=Path("capabilities"))

    def out(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", help="Machine-readable output")

    s = dsub.add_parser("scan", help="Every drift event on record")
    s.add_argument("--capability", help="Only this capability")
    s.add_argument("--kind", help="Only this kind (CONTROL_RENAMED, ...)")
    s.add_argument("--limit", type=int, default=20, help="Most recent N (0: all)")
    for f in (runs, caps, out):
        f(s)

    r = dsub.add_parser("report", help="Drift rates by capability, tenant and locator rung")
    r.add_argument(
        "--exclude-injected",
        action="store_true",
        help="Leave out runs with a fault injected into the app on purpose",
    )
    for f in (runs, caps, out):
        f(r)

    p = dsub.add_parser(
        "propose", help="Propose a candidate repair for the drift that stopped a run"
    )
    p.add_argument("run", help="A run id, or its directory")
    p.add_argument("--step", help="The step to repair (default: the first that broke)")
    p.add_argument("--tenant", default="local", help="Whose policy scrubs the new locators")
    p.add_argument("--policy", type=Path, default=Path("policies/default.yaml"))
    for f in (runs, caps, out):
        f(p)

    e = dsub.add_parser(
        "evaluate",
        help="Replay a candidate beside the version it repairs, on the benchmark tasks",
    )
    e.add_argument("name")
    e.add_argument("--version", type=int, help="Default: the newest candidate")
    e.add_argument("--suite", default="core")
    e.add_argument("--task", action="append", default=[], help="Only these tasks (repeatable)")
    e.add_argument("--repetitions", type=int, default=1, help="Runs per task and version")
    e.add_argument("--runs-root", type=Path, default=Path("bench/runs/candidates"))
    for f in (caps, out):
        f(e)

    c = dsub.add_parser("candidates", help="Candidate repairs and where each stands")
    c.add_argument("name", nargs="?")
    for f in (caps, out):
        f(c)

    w = dsub.add_parser("show", help="One candidate: its rationale, checks and evaluation")
    w.add_argument("name")
    w.add_argument("--version", type=int, help="Default: the newest candidate")
    for f in (caps, out):
        f(w)

    j = dsub.add_parser("reject", help="Turn a candidate down, with who and why")
    j.add_argument("name")
    j.add_argument("--version", type=int, required=True)
    j.add_argument("--by", required=True)
    j.add_argument("--reason", required=True)
    caps(j)


def main(args: argparse.Namespace) -> int:
    from cua.drift.store import CandidateError

    command = args.drift_command
    try:
        if command == "scan":
            return _scan(args)
        if command == "report":
            return _report(args)
        if command == "propose":
            return _propose(args)
        if command == "evaluate":
            return _evaluate(args)
        if command == "candidates":
            return _candidates(args)
        if command == "show":
            return _show(args)
        return _reject(args)
    except CandidateError as exc:
        print(f"cua drift {command}: {exc}", file=sys.stderr)
        return EX_USAGE if "no candidate" in str(exc) else 1


def _roots(args: argparse.Namespace) -> list[Path]:
    from cua.drift.detect import DEFAULT_ROOTS

    return list(args.runs_dir) or list(DEFAULT_ROOTS)


def _known(args: argparse.Namespace) -> Any:
    from cua.drift.detect import Known
    from cua.drift.store import Candidates
    from cua.registry.store import Registry, RegistryError

    known = Known()
    try:
        for v in Registry(args.capabilities_dir).all_versions():
            known.add(v.capability)
    except RegistryError as exc:
        print(f"cua drift: registry not read ({exc}); classifying from the logs", file=sys.stderr)
    for c in Candidates(args.capabilities_dir).all():
        known.add(c.capability)
    return known


def _scan(args: argparse.Namespace) -> int:
    from cua.drift.detect import scan

    runs = list(scan(_roots(args), _known(args)))
    events = sorted(
        (
            e
            for r in runs
            for e in r.events
            if (args.capability is None or e.capability == args.capability)
            and (args.kind is None or e.kind == args.kind.upper())
        ),
        key=lambda e: e.at,
    )
    shown = events[-args.limit :] if args.limit else events
    if args.json:
        print(json.dumps([e.model_dump(mode="json") for e in shown], indent=2))
        return 0
    for e in shown:
        injected = f"  [injected {e.injected}]" if e.injected else ""
        state = "stopped the run" if e.fatal else f"survived on {e.resolved_rung}"
        print(
            f"{e.at}  {e.capability} v{e.version}  tenant {e.tenant_id}  {e.step_id}  "
            f"{e.kind}, {state}{injected}"
        )
        rungs = ", ".join(e.observed_rungs)
        print(f"    {rungs + ': ' if rungs else ''}{e.reason}")
        if e.evidence_ref:
            print(f"    {e.evidence_ref}  (run {e.run_id})")
    more = f" (the last {len(shown)} shown; --limit 0 for all)" if len(shown) < len(events) else ""
    print(f"\n{len(events)} drift event(s) in {len(runs)} replay run(s){more}.")
    if any(e.fatal and e.kind == "CONTROL_RENAMED" for e in shown):
        print("A drift that stopped a run can be repaired: cua drift propose <run id>")
    return 0


def _report(args: argparse.Namespace) -> int:
    from cua.drift.aggregate import aggregate
    from cua.drift.detect import scan

    report = aggregate(scan(_roots(args), _known(args)), exclude_injected=args.exclude_injected)
    if args.json:
        print(report.model_dump_json(indent=2))
        return 0
    scope = ", injected faults left out" if args.exclude_injected else ""
    print(
        f"Drift over {report.runs} replay run(s) that reached the app ({report.invocations} "
        f"invocation(s){scope}): {report.drift_events} event(s), "
        f"{_pct(report.rate)} per invocation"
    )
    for title, rows, per in (
        ("By capability version", report.by_capability, "invocations"),
        ("By tenant", report.by_tenant, "invocations"),
        ("By recorded locator rung", report.by_rung, "lookups"),
    ):
        print(f"\n{title} (drift events / {per})")
        for row in rows:
            kinds = ", ".join(f"{k} {n}" for k, n in row.kinds.items())
            print(
                f"    {row.key:<32} {row.drift_events:>4} / {row.denominator:<5} "
                f"{_pct(row.rate):>7}   stopped {row.fatal}, injected {row.injected}"
                + (f"   {kinds}" if kinds else "")
            )
    if report.by_kind:
        print("\nBy kind: " + ", ".join(f"{k} {n}" for k, n in report.by_kind.items()))
    return 0


def _propose(args: argparse.Namespace) -> int:
    from cua.drift.candidate import propose
    from cua.observability.recorder import locate_run
    from cua.policy.allowlist import load_policy
    from cua.tenant import load_tenant

    run_dir = locate_run(args.run, [*_roots(args), Path("evidence/runs")])
    if run_dir is None:
        print(f"cua drift propose: no run {args.run!r} found", file=sys.stderr)
        return EX_USAGE
    try:
        policy = load_policy(args.policy, load_tenant(args.tenant))
    except (OSError, ValueError) as exc:
        print(f"cua drift propose: {exc}", file=sys.stderr)
        return EX_USAGE
    candidate, new = propose(
        run_dir, capabilities_dir=args.capabilities_dir, policy=policy, step=args.step
    )
    r = candidate.record
    if args.json:
        print(r.model_dump_json(indent=2))
        return 0
    verb = "proposed" if new else "already proposed as"
    print(
        f"cua drift propose: {verb} {r.name} v{r.version}, a candidate repair of "
        f"v{r.base.version} (a draft; v{r.base.version} is unchanged and still runs)"
    )
    event = r.drift[0]
    print(f"    {event.step_id}: {event.kind}: {event.reason}")
    for rung in r.change.added:
        print(f"    + {_rung(rung)}")
    print(f"    checks: {_checks(r.checks)}")
    print(f"    {link(candidate.dir / 'rationale.md', stream=sys.stdout)}")
    print(f"Next: cua drift evaluate {r.name} --version {r.version}")
    return 0


def _evaluate(args: argparse.Namespace) -> int:
    from cua.benchmark.registry import SuiteError
    from cua.drift.evaluate import evaluate
    from cua.drift.store import Candidates

    candidate = Candidates(args.capabilities_dir).get(args.name, args.version)

    def progress(side: str, row: Any) -> None:
        if not args.json:
            print(
                f"    {row.task_id:<32} {side:<9} #{row.repetition}  {row.match:<9} "
                f"{row.outcome}  {row.duration_s:.1f} s",
                flush=True,
            )

    if not args.json:
        print(f"cua drift evaluate: {candidate.label} beside v{candidate.record.base.version}")
    try:
        done = evaluate(
            candidate,
            capabilities_dir=args.capabilities_dir,
            suite=args.suite,
            task_ids=args.task,
            repetitions=args.repetitions,
            runs_root=args.runs_root,
            progress=progress,
        )
    except SuiteError as exc:
        print(f"cua drift evaluate: {exc}", file=sys.stderr)
        return EX_USAGE
    ev = done.record.evaluation
    assert ev is not None
    if args.json:
        print(ev.model_dump_json(indent=2))
        return 0 if ev.passed else 1
    print()
    for g in ev.gates:
        print(f"    {'PASS' if g.passed else 'FAIL'}  {g.id}: {g.detail}")
    print(f"    {link(done.dir / 'rationale.md', stream=sys.stdout)}")
    r = done.record
    if ev.passed:
        path = done.path.as_posix()
        print(
            f"Passed. Ready for review: cua describe {path}, then "
            f"cua approve {path} --by <your name>"
        )
        return 0
    print(f"Failed: {r.name} v{r.version} will not be approved as it is.")
    return 1


def _candidates(args: argparse.Namespace) -> int:
    from cua.drift.store import Candidates, status

    found = Candidates(args.capabilities_dir).all(args.name)
    registered = _registered(args)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": c.record.name,
                        "version": c.record.version,
                        "status": status(c, registered),
                        "base_version": c.record.base.version,
                        "kind": c.record.drift[0].kind,
                        "step_id": c.record.change.step_id,
                        "path": c.dir.as_posix(),
                    }
                    for c in found
                ],
                indent=2,
            )
        )
        return 0
    if not found:
        print("No candidate repairs." if args.name is None else f"No candidate for {args.name}.")
        return 0
    for c in found:
        r = c.record
        print(
            f"{r.name} v{r.version}  {status(c, registered):<17} repairs v{r.base.version}: "
            f"{r.drift[0].kind} at {r.change.step_id}  ({r.created_at})"
        )
    return 0


def _show(args: argparse.Namespace) -> int:
    from cua.drift.store import RATIONALE, Candidates, status

    c = Candidates(args.capabilities_dir).get(args.name, args.version)
    if args.json:
        print(c.record.model_dump_json(indent=2))
        return 0
    print(f"status: {status(c, _registered(args))}\n")
    print((c.dir / RATIONALE).read_text(encoding="utf-8"), end="")
    return 0


def _reject(args: argparse.Namespace) -> int:
    from cua.drift.models import Rejection
    from cua.drift.store import CandidateError, Candidates, status

    store = Candidates(args.capabilities_dir)
    c = store.get(args.name, args.version)
    by, reason = args.by.strip(), args.reason.strip()
    if not by or not reason:
        raise CandidateError("say who is rejecting it (--by) and why (--reason)")
    now = status(c, _registered(args))
    if now not in ("proposed", "ready for review", "failed evaluation"):
        raise CandidateError(f"{c.label} is {now}; only an undecided candidate is rejected")
    at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    store.write(
        c.dir, c.record.model_copy(update={"rejection": Rejection(by=by, at=at, reason=reason)})
    )
    print(f"cua drift reject: {c.label} is rejected ({link(c.dir, stream=sys.stdout)})")
    return 0


def _registered(args: argparse.Namespace) -> dict[tuple[str, int], tuple[str, str]]:
    from cua.registry.store import Registry, RegistryError

    try:
        return {
            (v.record.name, v.record.version): (v.status, v.record.artifact_hash)
            for v in Registry(args.capabilities_dir).all_versions()
        }
    except RegistryError:
        return {}


def _rung(rung: dict[str, Any]) -> str:
    frame = (rung.get("within") or {}).get("frame")
    where = f" (frame {frame!r})" if frame else ""
    if rung.get("strategy") == "role_name":
        return f'role_name {rung.get("role")} "{rung.get("name")}"{where}'
    if rung.get("strategy") == "near_text":
        return f'near_text "{rung.get("text")}" {rung.get("role") or ""}{where}'
    return json.dumps(rung, sort_keys=True)


def _checks(checks: list[Any]) -> str:
    from collections import Counter

    counts = Counter(c.result for c in checks)
    text = ", ".join(f"{n} {k}" for k, n in counts.items())
    flagged = [c.id for c in checks if c.result in ("fail", "attention")]
    return text + (f" ({', '.join(flagged)})" if flagged else "")


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"
