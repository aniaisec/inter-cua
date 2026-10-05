"""``cua serve``: the capability runtime over HTTP (``cua.api``)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cua.termlink import link

EX_USAGE = 64
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    s = sub.add_parser(
        "serve",
        help="Serve approved capabilities over HTTP",
        description="Invoke approved capabilities over HTTP and get the same ReplayResult as "
        "`cua replay`. Callers authenticate with an API key and name their tenant; "
        "api/access.yaml says which clients may do what.",
    )
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8200)
    s.add_argument("--access", type=Path, default=Path("api/access.yaml"))
    s.add_argument("--runs-dir", type=Path, default=Path("evidence/runs"))
    s.add_argument("--capabilities-dir", type=Path, default=Path("capabilities"))
    s.add_argument("--workers", type=int, default=2, help="Runs executing at once (2)")
    s.add_argument(
        "--operator-url",
        default="http://127.0.0.1:8100",
        help="Where the operator console is, for runs that ask for a handoff",
    )
    s.add_argument(
        "--allow-inject",
        action="store_true",
        help="Accept a request's `inject` (mock-app failure modes; demos only)",
    )


def main(args: argparse.Namespace) -> int:
    import uvicorn

    from cua.api.access import Gate, create_keys, load_access
    from cua.api.app import create_app
    from cua.api.service import RunService, ServiceSettings

    try:
        config = load_access(args.access, project=args.project)
        for path in create_keys(config):
            print(f"cua serve: created an API key at {link(path)}", file=sys.stderr)
        gate = Gate(config, project=args.project, environ=args.project.environ)
    except (OSError, ValueError) as exc:
        print(f"cua serve: {exc}", file=sys.stderr)
        return EX_USAGE
    if args.workers < 1:
        print("cua serve: --workers is at least 1", file=sys.stderr)
        return EX_USAGE
    for name, why in gate.disabled.items():
        print(f"cua serve: client {name!r} is disabled: {why}", file=sys.stderr)
    if not set(config.clients) - set(gate.disabled):
        print("cua serve: no client has a usable key; nobody could call this", file=sys.stderr)
        return EX_USAGE
    if args.host not in LOOPBACK:
        print(
            f"cua serve: WARNING: listening on {args.host} without TLS; API keys and approval "
            "tokens cross the network in the clear. Put TLS in front of it.",
            file=sys.stderr,
        )
    service = RunService(
        ServiceSettings(
            runs_dir=args.runs_dir,
            capabilities_dir=args.capabilities_dir,
            operator_url=args.operator_url,
            project=args.project,
            workers=args.workers,
            allow_inject=args.allow_inject,
        ),
        environ=args.project.environ,
    )
    print(
        f"cua serve: {link(f'http://{args.host}:{args.port}/docs')} (tenants: "
        f"{', '.join(gate.tenants)}; runs under {link(args.runs_dir)})",
        file=sys.stderr,
    )
    uvicorn.run(create_app(gate, service), host=args.host, port=args.port, log_level="warning")
    return 0
