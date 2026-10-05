"""``cua mcp``: approved capabilities as MCP tools, over stdio."""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

EX_USAGE = 64


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    m = sub.add_parser(
        "mcp",
        help="Serve approved capabilities as MCP tools over stdio",
        description="An MCP server on stdin/stdout: one tool per approved capability, "
        "invoked like `cua catalog invoke`. It acts as one client of api/access.yaml, for one "
        "tenant; the client's key is not asked for, because whoever can start this process "
        "already has the files.",
    )
    m.add_argument(
        "--root",
        type=Path,
        help="Select this project directory: an MCP client may start the server "
        "anywhere; explicit file arguments remain relative to the invocation directory",
    )
    m.add_argument("--client", default="local-agent", help="Client in the access file")
    m.add_argument("--tenant", default="local", help="One of the client's tenants")
    m.add_argument("--access", type=Path, default=Path("api/access.yaml"))
    m.add_argument("--runs-dir", type=Path, default=Path("evidence/runs"))
    m.add_argument("--capabilities-dir", type=Path, default=Path("capabilities"))
    m.add_argument(
        "--handoff",
        choices=("consent", "all", "none"),
        default="consent",
        help="Which runs may come back escalated with the session waiting: those that may ask "
        "for consent (default), all, or none",
    )
    m.add_argument(
        "--handoff-ttl",
        type=float,
        default=1800.0,
        metavar="S",
        help="Seconds a request stays open",
    )
    m.add_argument("--operator-url", default="http://127.0.0.1:8100", help=argparse.SUPPRESS)
    m.add_argument(
        "--wait", type=float, default=300.0, metavar="S", help="Longest a tool call waits (300)"
    )


def main(args: argparse.Namespace) -> int:
    from cua.api.access import Caller, Gate, load_access
    from cua.api.service import RunService, ServiceSettings
    from cua.mcp.server import McpServer

    try:
        config = load_access(args.access, project=args.project)
        gate = Gate(config, project=args.project, environ=args.project.environ)
    except (OSError, ValueError) as exc:
        print(f"cua mcp: {exc}", file=sys.stderr)
        return EX_USAGE
    client = config.clients.get(args.client)
    if client is None:
        print(f"cua mcp: no client {args.client!r} in {args.access.as_posix()}", file=sys.stderr)
        return EX_USAGE
    if args.tenant not in client.tenants:
        print(
            f"cua mcp: client {args.client!r} does not serve tenant {args.tenant!r}",
            file=sys.stderr,
        )
        return EX_USAGE
    if not 0 < args.wait <= 300:
        print("cua mcp: --wait is between 0 and 300 seconds", file=sys.stderr)
        return EX_USAGE
    tenant, policy = gate.tenants[args.tenant]

    # stdout is the protocol's. Anything else printed there (a runner warning,
    # a library's notice) would be a malformed message, so it goes to stderr.
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8-sig", newline=None)
    sys.stdout = sys.stderr

    service = RunService(
        ServiceSettings(
            runs_dir=args.runs_dir,
            capabilities_dir=args.capabilities_dir,
            operator_url=args.operator_url,
            project=args.project,
        ),
        environ=args.project.environ,
    )
    server = McpServer(
        service,
        lambda request_id: Caller(args.client, client, tenant, policy, request_id),
        wait_s=args.wait,
        handoff=args.handoff,
        handoff_ttl_s=args.handoff_ttl,
    )
    print(
        f"cua mcp: serving tenant {tenant.id} as client {args.client} on stdio",
        file=sys.stderr,
    )
    try:
        server.serve(stdin, out)
    finally:
        service.close()
    return 0
