"""``cua`` command line entry point.

The subcommand surface is declared here in full from M0 so that the shape of
the deliverable is fixed early and the README's CLI table has something to
point at. Everything except ``mockapp`` exits 70 (EX_SOFTWARE) naming the
milestone that will land it.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

PENDING: dict[str, str] = {
    "discover": "M2 - goal-driven agent loop",
    "describe": "M3 - capability artifact and recorder",
    "approve": "M3 - approval gate",
    "replay": "M4 - deterministic replay engine",
    "resume": "M6 - escalation and handoff",
    "operator": "M6 - escalation and handoff",
    "catalog": "M9 - stretch",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cua", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    mockapp = sub.add_parser("mockapp", help="Run the mock legacy target app")
    mockapp.add_argument("--host", default="127.0.0.1")
    mockapp.add_argument("--port", type=int, default=8000)

    for name, milestone in PENDING.items():
        sub.add_parser(name, help=f"(not yet implemented: {milestone})")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "mockapp":
        import uvicorn

        uvicorn.run("mockapp.app:app", host=args.host, port=args.port)
        return 0

    milestone = PENDING[args.command]
    print(f"cua {args.command}: not implemented yet ({milestone})", file=sys.stderr)
    return 70


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
