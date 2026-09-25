"""``cua registry list | show | versions | health | deprecate | revoke |
reinstate | sync``.

Reads the capability files and the ledger; the three lifecycle commands
append to the ledger, and ``sync`` registers approved capabilities that are
not registered yet. Health is read from run directories. Imports no model
client.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from cua.termlink import link

EX_USAGE = 64


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    r = sub.add_parser(
        "registry",
        help="Every version of every capability, its lifecycle, and its health",
        description="The capability registry: each version's lifecycle status (draft, "
        "review, approved, deprecated, revoked), who moved it and why, and its health "
        "from the replays on record. `cua catalog` is the approved view of it.",
    )
    rsub = r.add_subparsers(dest="registry_command", required=True)

    def common(p: argparse.ArgumentParser, *, runs: bool = False) -> None:
        p.add_argument("--capabilities-dir", type=Path, default=Path("capabilities"))
        p.add_argument("--json", action="store_true", help="Machine-readable output")
        if runs:
            p.add_argument(
                "--runs-dir",
                type=Path,
                action="append",
                default=[],
                help="Where to look for runs (repeatable; default: evidence/ and bench/runs/)",
            )

    common(rsub.add_parser("list", help="Every capability version and its status"))

    s = rsub.add_parser("show", help="One version: its record and health")
    s.add_argument("name")
    s.add_argument("--version", type=int, help="Default: the one `cua catalog` would run")
    common(s, runs=True)

    v = rsub.add_parser("versions", help="Every version of one capability")
    v.add_argument("name")
    common(v)

    h = rsub.add_parser("health", help="Each version's health, from its replays")
    h.add_argument("name")
    h.add_argument(
        "--include-injected",
        action="store_true",
        help="Count runs with a failure injected into the target app",
    )
    common(h, runs=True)

    for change, text in (
        ("deprecate", "Stop offering a version by default; it still runs when named"),
        ("revoke", "Stop a version from starting any new execution (final)"),
        ("reinstate", "Return a deprecated version to approved"),
    ):
        p = rsub.add_parser(change, help=text)
        p.add_argument("name")
        p.add_argument("--version", type=int, required=True)
        p.add_argument("--by", required=True, help="Who is making the change")
        p.add_argument("--reason", default="", help="Why (required to revoke; kept in the ledger)")
        common(p)

    common(rsub.add_parser("sync", help="Register approved capabilities not registered yet"))


def main(args: argparse.Namespace) -> int:
    from cua.registry.store import Registry, RegistryError

    registry = Registry(args.capabilities_dir)
    command = args.registry_command
    try:
        if command == "list":
            return _list(registry, args)
        if command == "show":
            return _show(registry, args)
        if command == "versions":
            return _versions(registry, args)
        if command == "health":
            return _health(registry, args)
        if command == "sync":
            return _sync(registry, args)
        return _change(registry, args)
    except RegistryError as exc:
        print(f"cua registry {command}: {exc}", file=sys.stderr)
        return EX_USAGE if "no capability named" in str(exc) or "has no v" in str(exc) else 1
    finally:
        for u in registry.unreadable:
            print(f"cua registry: skipped {u.path.as_posix()}: {u.error}", file=sys.stderr)


def _list(registry: Any, args: argparse.Namespace) -> int:
    from cua.registry.resolver import default_version

    versions = registry.all_versions()
    if args.json:
        print(json.dumps([v.record.model_dump(mode="json") for v in versions], indent=2))
        return 0
    if not versions:
        print(f"No capabilities in {args.capabilities_dir.as_posix()}.")
        return 0
    by_name: dict[str, list[Any]] = {}
    for v in versions:
        by_name.setdefault(v.record.name, []).append(v)
    for name, mine in by_name.items():
        default = default_version(mine)
        print(name)
        for v in mine:
            print("    " + _line(v.record, default=v is default and v.status == "approved"))
    print("\nOne version in full: cua registry show <name> [--version N]")
    return 0


def _line(r: Any, *, default: bool = False) -> str:
    where = " + ".join(
        w for w, on in (("registered", r.registered), ("working copy", r.working_copy)) if on
    )
    tags = [where]
    if default:
        tags.insert(0, "default")
    if r.edited_outside:
        tags.append("edited by hand")
    by = f" by {r.approved_by} {r.approved_at}" if r.approved_by else ""
    last = r.history[-1] if r.history else None
    change = ""
    if last is not None and last.status != "approved":
        change = f"; {last.status} by {last.by} {last.at}" + (
            f": {last.reason}" if last.reason else ""
        )
    return f"v{r.version}  {r.status:<10} [{', '.join(tags)}]{by}{change}"


def _show(registry: Any, args: argparse.Namespace) -> int:
    from cua.registry.health import health_by_version, replays_of, summary
    from cua.registry.resolver import Unresolvable, resolve

    try:
        v = resolve(registry, args.name, args.version)
    except Unresolvable as exc:
        print(f"cua registry show: {exc}", file=sys.stderr)
        return EX_USAGE
    health = health_by_version(args.name, replays_of(args.name, _roots(args)))
    h = health.get(v.record.version)
    record = v.record.model_copy(update={"health": summary(h) if h else None})
    if args.json:
        print(record.model_dump_json(indent=2))
        return 0
    r = record
    print(f"{r.name}  v{r.version}  {r.status}")
    print(f"    {r.description}")
    tenants = ", ".join(r.tenant_scope) or "-"
    print(f"    app family {r.app_family} ({r.surface}); tenants: {tenants}")
    print(f"    side effects: {r.side_effects}")
    print(f"    content {r.artifact_hash[:12]}, recorded {r.created_at}")
    if r.approved_by:
        print(f"    approved by {r.approved_by} {r.approved_at}")
    for e in r.history:
        why = f": {e.reason}" if e.reason else ""
        print(f"    {e.at}  {e.previous} -> {e.status} by {e.by}{why}")
    if r.health is None:
        print("    health: no replay of this version on record")
    else:
        hh = r.health
        print(
            f"    health over {hh['runs']} replay(s): success {_pct(hh['success_rate'])}, "
            f"drift {_pct(hh['drift_rate'])}, human {_pct(hh['human_intervention_rate'])}, "
            f"escalation {_pct(hh['escalation_rate'])}, p95 {hh['p95_latency_s']:.2f} s"
        )
    print(f"    {link(Path(r.path), stream=sys.stdout)}")
    return 0


def _versions(registry: Any, args: argparse.Namespace) -> int:
    from cua.registry.resolver import default_version

    mine = registry.versions(args.name)
    if not mine:
        print(f"cua registry versions: {registry.unknown(args.name)}", file=sys.stderr)
        return EX_USAGE
    if args.json:
        print(json.dumps([v.record.model_dump(mode="json") for v in mine], indent=2))
        return 0
    default = default_version(mine)
    for v in mine:
        print(_line(v.record, default=v is default and v.status == "approved"))
    return 0


def _health(registry: Any, args: argparse.Namespace) -> int:
    from cua.observability.cli import health_table
    from cua.registry.health import health_by_version, replays_of

    mine = {v.record.version: v.status for v in registry.versions(args.name)}
    runs = replays_of(args.name, _roots(args))
    if not mine and not runs:
        print(f"cua registry health: {registry.unknown(args.name)}", file=sys.stderr)
        return EX_USAGE
    health = health_by_version(args.name, runs, include_injected=args.include_injected)
    if args.json:
        out = [
            {
                "version": version,
                "status": mine.get(version, "not on disk"),
                "health": health[version].model_dump(mode="json") if version in health else None,
            }
            for version in sorted(set(mine) | set(health))
        ]
        print(json.dumps(out, indent=2))
        return 0
    for version in sorted(set(mine) | set(health)):
        status = mine.get(version, "not on disk (an earlier version; runs kept)")
        seen = "" if version in health else ": no replay on record"
        print(f"v{version}  {status}{seen}")
    if health:
        print()
        print("\n".join(health_table(health.values())))
    return 0


def _change(registry: Any, args: argparse.Namespace) -> int:
    record = registry.transition(
        args.name, args.version, args.registry_command, by=args.by, reason=args.reason
    )
    if args.json:
        print(record.model_dump_json(indent=2))
        return 0
    print(
        f"cua registry {args.registry_command}: {record.name} v{record.version} is "
        f"{record.status} ({link(registry.ledger_path, stream=sys.stdout)})"
    )
    return 0


def _sync(registry: Any, args: argparse.Namespace) -> int:
    written = registry.sync()
    if args.json:
        print(json.dumps([p.as_posix() for p in written]))
        return 0
    for p in written:
        print(f"cua registry sync: registered {link(p, stream=sys.stdout)}")
    if not written:
        print("cua registry sync: every approved capability is registered")
    return 0


def _roots(args: argparse.Namespace) -> list[Path]:
    from cua.registry.health import DEFAULT_ROOTS

    return list(args.runs_dir) or list(DEFAULT_ROOTS)


def _pct(v: float) -> str:
    return f"{v * 100:.1f}%"
