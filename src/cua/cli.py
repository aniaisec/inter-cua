"""``cua`` command line entry point.

The subcommand surface is declared here in full from M0 so that the shape of
the deliverable is fixed early and the README's CLI table has something to
point at. Subcommands not built yet exit 70 (EX_SOFTWARE) naming the milestone
that will land them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

PENDING: dict[str, str] = {
    "describe": "M3 - capability artifact and recorder",
    "approve": "M3 - approval gate",
    "replay": "M4 - deterministic replay engine",
    "resume": "M6 - escalation and handoff",
    "operator": "M6 - escalation and handoff",
    "catalog": "M9 - stretch",
}

EX_USAGE = 64


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cua", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    mockapp = sub.add_parser("mockapp", help="Run the mock legacy target app")
    mockapp.add_argument("--host", default="127.0.0.1")
    mockapp.add_argument("--port", type=int, default=8000)

    _add_discover(sub)

    for name, milestone in PENDING.items():
        sub.add_parser(name, help=f"(not yet implemented: {milestone})")

    return parser


def _add_discover(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    d = sub.add_parser(
        "discover",
        help="Goal-driven discovery run: an agent works the UI until the goal is reached",
        description="Drive the target with an LLM agent until the goal is reached, and "
        "record the run under evidence/runs/<run_id>/. Exit 0 done, 3 escalated, 1 stopped.",
    )
    d.add_argument("--goal", required=True, help="What to accomplish, in plain words")
    d.add_argument("--name", help="Capability name to record the run under (default: from goal)")
    d.add_argument("--tenant", default="local", help="tenants/<id>.yaml, or a path to one")
    d.add_argument("--entry", default="/", help="Start path on the tenant (default: /)")
    d.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME[:TYPE]=VALUE",
        help="Declared input and the value to discover with, e.g. member_id:string=10003",
    )
    d.add_argument(
        "--output",
        action="append",
        default=[],
        metavar="NAME[:TYPE][?][=DESCRIPTION]",
        help="Declared output, e.g. savings_balance:decimal; a trailing ? makes it optional",
    )
    d.add_argument(
        "--credential",
        action="append",
        default=[],
        metavar="NAME=secret://...",
        help="Credential the agent may type by placeholder (default: the tenant's only "
        "secret, as app_login)",
    )
    d.add_argument("--policy", default="policies/default.yaml", type=Path)
    d.add_argument("--llm", choices=("anthropic", "scripted"), default="anthropic")
    d.add_argument("--script", type=Path, help="Tool-call script for --llm scripted")
    d.add_argument("--model", help="Model id (default: $CUA_MODEL, else claude-sonnet-5)")
    d.add_argument("--max-steps", type=int, default=30)
    d.add_argument("--timeout-s", type=float, default=600.0)
    d.add_argument("--no-screenshots", action="store_true", help="Send the tree only")
    d.add_argument(
        "--auto-approve-risky",
        action="store_true",
        help="Let risky actions through without approval. Logged loudly; not for evidence "
        "runs. Replaced by the approval handoff in M6.",
    )
    d.add_argument("--inject", help="Arm a mock-app failure mode for this run")
    d.add_argument("--headed", action="store_true", help="Show the browser window")
    d.add_argument("--runs-dir", type=Path, default=Path("evidence/runs"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "mockapp":
        import uvicorn

        uvicorn.run("mockapp.app:app", host=args.host, port=args.port)
        return 0

    if args.command == "discover":
        load_dotenv(Path(".env"))
        return _discover(args)

    milestone = PENDING[args.command]
    print(f"cua {args.command}: not implemented yet ({milestone})", file=sys.stderr)
    return 70


def _discover(args: argparse.Namespace) -> int:
    # Imported here so that `cua --help` and the pending subcommands do not
    # pay for Playwright, or for the model SDK.
    from cua.agent.goal import Goal, SpecError, parse_credential, parse_output, parse_param
    from cua.agent.llm import AnthropicMessagesClient, LLMClient, ScriptedClient
    from cua.agent.loop import DiscoveryConfig, DiscoveryLoop
    from cua.agent.script import load_script
    from cua.agent.stopping import StopLimits
    from cua.evidence.logger import RunLog
    from cua.policy.allowlist import load_policy
    from cua.secrets.resolver import SecretError, resolve
    from cua.surface.playwright_surface import PlaywrightSurface
    from cua.tenant import load_tenant

    try:
        tenant = load_tenant(args.tenant)
        credentials_spec = dict(parse_credential(c) for c in args.credential)
        if not credentials_spec and len(tenant.secrets) == 1:
            (key,) = tenant.secrets
            credentials_spec = {"app_login": f"secret://{tenant.id}/{key}"}
        goal = Goal(
            goal=args.goal,
            name=args.name or _slug(args.goal),
            entry=args.entry,
            params=[parse_param(p) for p in args.param],
            outputs=[parse_output(o) for o in args.output],
            credentials=credentials_spec,
        )
        credentials = {name: resolve(ref, tenant) for name, ref in credentials_spec.items()}
        policy = load_policy(args.policy, tenant)
    except (SpecError, SecretError, OSError, ValueError) as exc:
        print(f"cua discover: {exc}", file=sys.stderr)
        return EX_USAGE

    llm: LLMClient
    if args.llm == "scripted":
        if args.script is None:
            print("cua discover: --llm scripted needs --script", file=sys.stderr)
            return EX_USAGE
        llm = ScriptedClient(load_script(args.script))
    else:
        llm = AnthropicMessagesClient(args.model)

    entry_url = tenant.url(goal.entry)
    if args.inject:
        entry_url += ("&" if "?" in entry_url else "?") + f"inject={args.inject}"

    log = RunLog.create(args.runs_dir)
    print(f"cua discover: run {log.run_id} -> {log.dir.as_posix()}", file=sys.stderr)
    config = DiscoveryConfig(
        limits=StopLimits(max_steps=args.max_steps, timeout_s=args.timeout_s),
        screenshots=not args.no_screenshots,
        auto_approve_risky=args.auto_approve_risky,
    )
    with PlaywrightSurface.launch(headed=args.headed or None) as surface:
        outcome = DiscoveryLoop(
            surface=surface,
            llm=llm,
            goal=goal,
            tenant=tenant,
            policy=policy,
            credentials=credentials,
            log=log,
            config=config,
            entry_url=entry_url,
        ).run()

    summary = {
        "run_id": outcome.run_id,
        "kind": outcome.kind,
        "reason": outcome.reason,
        "message": outcome.message,
        "outputs": {name: o.normalized for name, o in outcome.outputs.items()},
        "steps": outcome.steps,
        "duration_ms": outcome.duration_ms,
        "run_dir": outcome.run_dir,
    }
    print(json.dumps(summary, indent=2))
    return outcome.exit_code


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines into the environment, never overriding what is set.

    Empty values are skipped, so an unfilled ``ANTHROPIC_API_KEY=`` does not
    shadow a key exported in the shell.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if value and key not in os.environ:
            os.environ[key] = value


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:6]
    return "_".join(words) or "capability"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
