"""``cua`` command line entry point.

Discover a capability, review and approve it, replay it deterministically,
hand a stuck run to a person and carry it on, and offer the approved ones to
a calling agent as tools (``cua catalog``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from cua.termlink import link

EX_USAGE = 64


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cua", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    mockapp = sub.add_parser("mockapp", help="Run the mock legacy target app")
    mockapp.add_argument("--host", default="127.0.0.1")
    mockapp.add_argument("--port", type=int, default=8000)

    _add_discover(sub)
    _add_artifact_commands(sub)
    _add_replay(sub)
    _add_handoff_commands(sub)
    _add_catalog(sub)

    from cua.benchmark.cli import add_parser as _add_benchmark

    _add_benchmark(sub)

    from cua.observability.cli import add_parser as _add_metrics

    _add_metrics(sub)

    from cua.registry.cli import add_parser as _add_registry

    _add_registry(sub)

    from cua.drift.cli import add_parser as _add_drift

    _add_drift(sub)

    from cua.workflow.cli import add_parser as _add_workflow

    _add_workflow(sub)

    from cua.security.cli import add_parser as _add_security

    _add_security(sub)
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
        help="Credential the agent may type by placeholder (default: the tenant's only app "
        "secret, as app_login)",
    )
    d.add_argument("--policy", default="policies/default.yaml", type=Path)
    d.add_argument(
        "--llm",
        choices=("auto", "anthropic", "gemini", "scripted"),
        default="auto",
        help="Model provider. auto (default) uses $CUA_LLM if set, else whichever of "
        "ANTHROPIC_API_KEY / GEMINI_API_KEY is present, Claude first",
    )
    d.add_argument("--script", type=Path, help="Tool-call script for --llm scripted")
    d.add_argument(
        "--model",
        help="Model id for the chosen provider (default: $CUA_MODEL for Claude, "
        "$CUA_GEMINI_MODEL for Gemini)",
    )
    d.add_argument("--max-steps", type=int, default=30)
    d.add_argument("--timeout-s", type=float, default=600.0)
    d.add_argument("--no-screenshots", action="store_true", help="Send the tree only")
    d.add_argument(
        "--auto-approve-risky",
        action="store_true",
        help="Let risky actions through without approval. Logged loudly; not for evidence "
        "runs. Prefer --handoff, which asks a person on the operator console.",
    )
    _add_handoff_flags(d)
    d.add_argument("--inject", help="Arm a mock-app failure mode for this run")
    d.add_argument("--headed", action="store_true", help="Show the browser window")
    d.add_argument("--runs-dir", type=Path, default=Path("evidence/runs"))
    d.add_argument(
        "--capabilities-dir",
        type=Path,
        default=Path("capabilities"),
        help="Where a run that reaches done is recorded, as <name>.json (draft)",
    )
    d.add_argument("--no-record", action="store_true", help="Keep the run; record nothing")


def _add_replay(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    r = sub.add_parser(
        "replay",
        help="Run an approved capability deterministically, with no model",
        description="Replay a capability against a tenant and print a JSON ReplayResult. "
        "Exit 0 success, 2 business outcome, 1 failure, 3 escalated.",
    )
    r.add_argument("capability", type=Path)
    r.add_argument("--tenant", default="local", help="tenants/<id>.yaml, or a path to one")
    _add_invocation_flags(r)
    r.add_argument(
        "--allow-draft",
        action="store_true",
        help="Operator override: replay a capability nobody has approved. Logged loudly.",
    )


def _add_invocation_flags(r: argparse.ArgumentParser) -> None:
    r.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="An input the capability declares, e.g. member_id=10003",
    )
    r.add_argument("--policy", default="policies/default.yaml", type=Path)
    r.add_argument(
        "--approval-token",
        help="Consent for the capability's risky steps, for this invocation only "
        "(from `cua approval-token`)",
    )
    r.add_argument(
        "--approved-by", help="Who gave that consent; refused if the token names someone else"
    )
    r.add_argument(
        "--idempotency-key",
        help="Same key and inputs again returns the stored result instead of running",
    )
    r.add_argument(
        "--budget",
        default="",
        metavar="timeout_s=120,max_recoveries=3,allow_escalation=true",
        help="Invocation limits",
    )
    r.add_argument("--step-timeout", type=float, default=3.0, help="Seconds one step may take")
    r.add_argument("--inject", help="Arm a mock-app failure mode for this run (demo)")
    r.add_argument("--no-screenshots", action="store_true")
    r.add_argument("--headed", action="store_true", help="Show the browser window")
    r.add_argument("--runs-dir", type=Path, default=Path("evidence/runs"))
    _add_handoff_flags(r)


def _add_handoff_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--handoff",
        action="store_true",
        help="Hand a stuck run to a person instead of failing: open an intervention request "
        "on the operator console (`cua operator`) and wait. The browser runs detached, so an "
        "unanswered request leaves the session up for `cua resume`.",
    )
    p.add_argument(
        "--handoff-wait",
        type=float,
        default=600.0,
        metavar="S",
        help="Seconds to wait for someone to take a request before returning escalated (600)",
    )
    p.add_argument(
        "--handoff-ttl",
        type=float,
        default=1800.0,
        metavar="S",
        help="Seconds a request stays open at all before it is aborted (1800)",
    )
    p.add_argument("--operator-url", default="http://127.0.0.1:8100", help=argparse.SUPPRESS)


def _handoff_settings(args: argparse.Namespace) -> Any:
    from cua.escalation.channel import HandoffSettings

    if not args.handoff:
        return None
    return HandoffSettings(
        wait_s=args.handoff_wait,
        ttl_s=args.handoff_ttl,
        operator_url=args.operator_url,
        announce=True,
    )


def _add_handoff_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    r = sub.add_parser(
        "resume",
        help="Carry on a run that returned escalated, once a person has handed back",
        description="Carry on an escalated replay from its resume token. If the person has "
        "not handed back on the console, this is the handback. The run checks the screen "
        "against its checkpoints and continues from the newest that holds (or asks again). "
        "Prints a JSON ReplayResult; exit codes as for replay.",
    )
    r.add_argument("resume_token")
    r.add_argument(
        "--resume-at", metavar="STEP_ID", help="Where to carry on (checked, not trusted)"
    )
    r.add_argument("--by", default="cua-resume", help="Who is handing back (cua-resume)")
    r.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Sensitive inputs again: they were not kept",
    )
    r.add_argument(
        "--approval-token",
        help="Consent for the risky step the run stopped at (NEEDS_APPROVAL), from "
        "`cua approval-token` with the run's inputs; instead of approving on the console",
    )
    r.add_argument(
        "--approved-by", help="Who gave that consent; refused if the token names someone else"
    )
    r.add_argument(
        "--handoff-wait",
        type=float,
        default=None,
        metavar="S",
        help="If it asks again, seconds to wait for someone (default: as the run started)",
    )
    r.add_argument("--runs-dir", type=Path, default=Path("evidence/runs"))

    o = sub.add_parser(
        "operator",
        help="Serve the operator console for intervention requests (:8100)",
        description="List open intervention requests; take control of the live session, "
        "resume, retry, approve or abort. Records what the person does.",
    )
    o.add_argument("--host", default="127.0.0.1")
    o.add_argument("--port", type=int, default=8100)
    o.add_argument("--runs-dir", type=Path, default=Path("evidence/runs"))
    o.add_argument("--tenant", default="local", help="For the policy's scrub patterns")
    o.add_argument("--policy", default="policies/default.yaml", type=Path)


def _add_catalog(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = sub.add_parser(
        "catalog",
        help="The approved capabilities as tools a calling agent can invoke by name",
        description="List the approved capabilities; --json prints them as tool definitions "
        "(name, description, input_schema) for a model's tool-calling API. "
        "`cua catalog invoke <name>` calls one with typed arguments and prints a JSON "
        "ReplayResult.",
    )
    c.add_argument("--json", action="store_true", help="Tool definitions, for an agent")
    c.add_argument(
        "--all",
        action="store_true",
        help="Drafts, deprecated and revoked capabilities too (not offered as tools)",
    )
    c.add_argument("--capabilities-dir", type=Path, default=Path("capabilities"))
    csub = c.add_subparsers(dest="catalog_command")
    i = csub.add_parser(
        "invoke",
        help="Invoke an approved capability by name",
        description="Invoke an approved capability by name. Arguments come as a JSON object "
        "typed the way a model's tool call types them (--args), as NAME=VALUE (--input), or "
        "inside a whole invocation request (--request FILE, or - for stdin). Prints a JSON "
        "ReplayResult; exit 0 success, 2 business outcome, 1 failure, 3 escalated.",
    )
    i.add_argument("name", nargs="?", help="Capability name (default: the request's)")
    i.add_argument(
        "--version",
        type=int,
        help="Run this version (default: the highest approved; a deprecated one runs "
        "only when named)",
    )
    i.add_argument("--args", metavar="JSON", help='Typed arguments, e.g. {"member_id": "10003"}')
    i.add_argument("--request", metavar="FILE", help="A JSON invocation request; - reads stdin")
    i.add_argument("--tenant", help="tenants/<id>.yaml, or a path to one (default: local)")
    i.add_argument("--capabilities-dir", type=Path, default=Path("capabilities"))
    _add_invocation_flags(i)


def _add_artifact_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    r = sub.add_parser(
        "record",
        help="Turn a finished discovery run into a draft capability",
        description="Read a run directory and write capabilities/<name>.json as a draft. "
        "Recording over an existing file makes a new version of that capability.",
    )
    r.add_argument("run_dir", type=Path)
    r.add_argument("--out", type=Path, help="Default: capabilities/<name>.json")
    r.add_argument("--policy", default="policies/default.yaml", type=Path)
    r.add_argument("--families-dir", type=Path, default=Path("capabilities/families"))

    d = sub.add_parser(
        "describe", help="Show a capability in plain words; required reading before approve"
    )
    d.add_argument("capability", type=Path)
    d.add_argument("--state-dir", type=Path, default=Path(".cua"), help=argparse.SUPPRESS)

    a = sub.add_parser(
        "approve",
        help="Approve a capability (draft -> approved) exactly as last described",
        description="Refuses unless the capability is exactly what `cua describe` last showed.",
    )
    a.add_argument("capability", type=Path)
    a.add_argument("--by", required=True, help="Who is approving")
    a.add_argument("--state-dir", type=Path, default=Path(".cua"), help=argparse.SUPPRESS)

    t = sub.add_parser(
        "approval-token",
        help="Sign consent for one invocation of a capability's risky steps",
        description="Print an approval token for exactly this capability (version and "
        "content), tenant and inputs, valid for --ttl-s seconds and for one commit. Pass it "
        "to `cua replay --approval-token`. Signed with the tenant's "
        "secret://<tenant>/cua/approval-signing-key; a file-bound key that does not exist "
        "yet is created.",
    )
    t.add_argument("capability", type=Path)
    t.add_argument("--tenant", default="local", help="tenants/<id>.yaml, or a path to one")
    t.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Exactly the inputs the replay will be given",
    )
    t.add_argument("--by", required=True, help="Who is giving consent")
    t.add_argument("--ttl-s", type=int, default=900, help="Seconds the token is valid (900)")

    s = sub.add_parser("schema", help="Write the capability JSON Schema")
    s.add_argument("--out", type=Path, default=Path("capabilities/schema/capability-1.1.json"))


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
    if args.command == "replay":
        load_dotenv(Path(".env"))
        return _replay(args)
    if args.command == "resume":
        load_dotenv(Path(".env"))
        return _resume(args)
    if args.command == "operator":
        return _operator(args)
    if args.command == "record":
        return _record(args)
    if args.command == "describe":
        return _describe(args)
    if args.command == "approve":
        return _approve(args)
    if args.command == "approval-token":
        load_dotenv(Path(".env"))
        return _approval_token(args)
    if args.command == "benchmark":
        load_dotenv(Path(".env"))
        from cua.benchmark.cli import main as benchmark

        return benchmark(args)
    if args.command == "metrics":
        from cua.observability.cli import main as metrics

        return metrics(args)
    if args.command == "registry":
        from cua.registry.cli import main as registry

        return registry(args)
    if args.command == "drift":
        load_dotenv(Path(".env"))
        from cua.drift.cli import main as drift

        return drift(args)
    if args.command == "security":
        from cua.security.cli import main as security

        return security(args)
    if args.command == "workflow":
        load_dotenv(Path(".env"))
        from cua.workflow.cli import main as workflow

        return workflow(args)
    if args.command == "catalog":
        if args.catalog_command == "invoke":
            load_dotenv(Path(".env"))
            return _catalog_invoke(args)
        return _catalog_list(args)

    from cua.artifact.schema import export_json_schema

    print(link(export_json_schema(args.out), stream=sys.stdout))
    return 0


def _discover(args: argparse.Namespace) -> int:
    # Imported here so that `cua --help` and the pending subcommands do not
    # pay for Playwright, or for the model SDK.
    from cua.agent.goal import Goal, SpecError, parse_credential, parse_output, parse_param
    from cua.agent.llm import LLMClient, NoProviderError, ScriptedClient, select_client
    from cua.agent.loop import DiscoveryConfig, DiscoveryLoop
    from cua.agent.script import load_script
    from cua.agent.stopping import StopLimits
    from cua.evidence.logger import RunLog
    from cua.policy.allowlist import load_policy
    from cua.secrets.resolver import SecretError, resolve
    from cua.surface.playwright_surface import PlaywrightSurface
    from cua.tenant import SYSTEM_SECRET_PREFIX, load_tenant

    try:
        tenant = load_tenant(args.tenant)
        credentials_spec = dict(parse_credential(c) for c in args.credential)
        if not credentials_spec and len(tenant.app_secrets) == 1:
            (key,) = tenant.app_secrets
            credentials_spec = {"app_login": f"secret://{tenant.id}/{key}"}
        for name, ref in credentials_spec.items():
            if ref.partition("://")[2].partition("/")[2].startswith(SYSTEM_SECRET_PREFIX):
                raise SpecError(f"{name}: {ref} is the system's own secret, never an app login")
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
        try:
            llm = select_client(args.llm, args.model)
        except NoProviderError as exc:
            print(f"cua discover: {exc}", file=sys.stderr)
            return EX_USAGE

    entry_url = tenant.url(goal.entry)
    if args.inject:
        entry_url += ("&" if "?" in entry_url else "?") + f"inject={args.inject}"

    log = RunLog.create(args.runs_dir)
    print(
        f"cua discover: run {log.run_id} ({llm.provider}: {llm.model}) -> {link(log.dir)}",
        file=sys.stderr,
    )
    config = DiscoveryConfig(
        limits=StopLimits(max_steps=args.max_steps, timeout_s=args.timeout_s),
        screenshots=not args.no_screenshots,
        auto_approve_risky=args.auto_approve_risky,
    )
    try:
        with PlaywrightSurface.launch(headed=args.headed or None) as surface:
            surface.restrict_egress(policy.allowed_origins)
            driven: Any = surface
            channel = None
            handoff = _handoff_settings(args)
            if handoff is not None:
                from cua.escalation.channel import OperatorChannel
                from cua.escalation.controller import ControlStore
                from cua.escalation.lease import LeasedSurface

                control = ControlStore(log.dir)
                control.start(log.run_id)
                channel = OperatorChannel(
                    log=log,
                    runs_dir=args.runs_dir,
                    control=control,
                    session=surface.expose,
                    kind="discovery",
                    capability=goal.name,
                    capability_version=None,
                    tenant=tenant.id,
                    settings=handoff,
                    while_waiting=surface.egress_lifted,
                )
                driven = LeasedSurface(surface, control.lease)
            outcome = DiscoveryLoop(
                surface=driven,
                llm=llm,
                goal=goal,
                tenant=tenant,
                policy=policy,
                credentials=credentials,
                log=log,
                config=config,
                entry_url=entry_url,
                handoff=channel,
            ).run()
            if channel is not None:
                channel.control.end(outcome.kind)
    except KeyboardInterrupt:
        import logging

        # The interrupted Playwright call leaves tasks behind that asyncio
        # reports at exit; the run's own record is what matters.
        logging.getLogger("asyncio").setLevel(logging.CRITICAL)
        print(
            f"cua discover: interrupted; the run says so in {link(log.dir / 'result.json')}",
            file=sys.stderr,
        )
        return 130

    summary = {
        "run_id": outcome.run_id,
        "kind": outcome.kind,
        "reason": outcome.reason,
        "message": outcome.message,
        "outputs": {name: o.normalized for name, o in outcome.outputs.items()},
        "steps": outcome.steps,
        "duration_ms": outcome.duration_ms,
        "handoffs": [h.model_dump(mode="json") for h in outcome.handoffs],
        "human_assisted": outcome.human_assisted,
        "run_dir": outcome.run_dir,
    }
    if outcome.kind == "done" and not args.no_record:
        from cua.artifact.recorder import RecordError

        out = args.capabilities_dir / f"{goal.name}.json"
        try:
            _record_run(log.dir, out, args.policy)
            summary["capability"] = out.as_posix()
        except RecordError as exc:
            print(f"cua discover: the run could not be recorded: {exc}", file=sys.stderr)
    print(json.dumps(summary, indent=2))
    return outcome.exit_code


def _replay(args: argparse.Namespace) -> int:
    try:
        inputs = dict(_pair(i, "--input") for i in args.input)
    except ValueError as exc:
        print(f"cua replay: {exc}", file=sys.stderr)
        return EX_USAGE
    return _invoke(
        "replay",
        args,
        args.capability,
        tenant_id=args.tenant,
        inputs=inputs,
        allow_draft=args.allow_draft,
    )


def _invoke(
    command: str,
    args: argparse.Namespace,
    capability: Path,
    *,
    tenant_id: str,
    inputs: dict[str, str],
    request: dict[str, Any] | None = None,
    allow_draft: bool = False,
) -> int:
    """One replay from the invocation flags, and for ``catalog invoke`` a
    request object too; a flag given on the command line wins over it."""
    from pydantic import ValidationError

    from cua.policy.allowlist import load_policy
    from cua.replay.engine import ReplayConfig
    from cua.replay.invocation import ApprovalGrant, Budget, Invocation
    from cua.replay.runner import InvocationError, launched, replay
    from cua.tenant import load_tenant

    request = request or {}
    try:
        tenant = load_tenant(tenant_id)
        policy = load_policy(args.policy, tenant)
        budget = Budget.model_validate(
            {
                **request.get("budget", {}),
                **dict(_pair(b, "--budget") for b in args.budget.split(",") if b.strip()),
            }
        )
        approval: ApprovalGrant | None = None
        if args.approval_token:
            approval = ApprovalGrant(token=args.approval_token, approved_by=args.approved_by)
        elif request.get("approval"):
            approval = ApprovalGrant.model_validate(request["approval"])
        handoff = _handoff_settings(args)
        invocation = Invocation(
            inputs=inputs,
            idempotency_key=args.idempotency_key or request.get("idempotency_key"),
            approval=approval,
            budget=budget,
            inject=args.inject,
        )
        result = replay(
            capability,
            tenant=tenant,
            policy=policy,
            invocation=invocation,
            runs_dir=args.runs_dir,
            allow_draft=allow_draft,
            config=ReplayConfig(
                step_timeout_s=args.step_timeout, screenshots=not args.no_screenshots
            ),
            surface=lambda: launched(headed=args.headed or None, detached=handoff is not None),
            handoff=handoff,
        )
    except (InvocationError, OSError, ValueError, ValidationError) as exc:
        print(f"cua {command}: {exc}", file=sys.stderr)
        return EX_USAGE
    except KeyboardInterrupt:
        import logging

        logging.getLogger("asyncio").setLevel(logging.CRITICAL)
        print(f"cua {command}: interrupted", file=sys.stderr)
        return 130
    return _print_result(command, result)


def _catalog_list(args: argparse.Namespace) -> int:
    from cua import catalog

    entries, broken = catalog.scan(args.capabilities_dir)
    for b in broken:
        print(f"cua catalog: skipped {b.path.as_posix()}: {b.error}", file=sys.stderr)
    if args.json:
        print(json.dumps(catalog.tools(entries, include_drafts=args.all), indent=2))
        return 0
    shown = [e for e in entries if e.invocable or args.all]
    if not shown:
        print(f"No approved capabilities in {args.capabilities_dir.as_posix()}.")
    for e in shown:
        cap = e.capability
        state = e.status + (" (edited by hand)" if e.edited_outside else "")
        ins = ", ".join(f"{n}: {s.type}{'' if s.required else '?'}" for n, s in cap.inputs.items())
        outs = ", ".join(
            f"{n}: {o.type}{'?' if o.optional else ''}" for n, o in cap.outputs.items()
        )
        print(f"{cap.name}  v{cap.version}  {state}  side effects: {cap.contract.side_effects}")
        print(f"    {cap.description}")
        print(f"    ({ins}) -> ({outs})")
        if cap.contract.outcomes:
            print(f"    business outcomes: {', '.join(sorted(cap.contract.outcomes))}")
        if cap.contract.may_escalate:
            print("    needs consent to commit: an approval token, or a person on the console")
        if not e.invocable and e.status == "approved":
            print(f"    not invocable here: this build has no {cap.target.surface} adapter")
        if e.versions > 1:
            print(f"    {e.versions} versions: cua registry versions {cap.name}")
        print(f"    {link(e.path, stream=sys.stdout)}")
    hidden = len(entries) - len(shown)
    if hidden:
        print(
            f"({hidden} not shown: drafts, deprecated or revoked, none of which is offered "
            "as a tool. --all lists them.)"
        )
    if shown:
        print("\nInvoke one by name: cua catalog invoke <name> --input name=value")
        print("Tool definitions for an agent: cua catalog --json")
    return 0


def _catalog_invoke(args: argparse.Namespace) -> int:
    from cua import catalog
    from cua.artifact.store import ArtifactError, open_capability
    from cua.replay.result import exit_code, to_json

    try:
        request: dict[str, Any] = {}
        if args.request is not None:
            text = (
                sys.stdin.read()
                if args.request == "-"
                else Path(args.request).read_text(encoding="utf-8-sig")
            )
            request = catalog.parse_request(text)
        asked = request.get("capability")
        if args.name and asked not in (None, args.name):
            raise ValueError(f"the request is for {asked!r}, not {args.name!r}")
        name = args.name or asked
        if not name:
            raise ValueError("name the capability to invoke (or give one in the request)")
        path = catalog.find(args.capabilities_dir, name, args.version)
        cap = open_capability(path).capability
        arguments: dict[str, Any] = dict(request.get("inputs", {}))
        if args.args is not None:
            arguments.update(catalog.parse_arguments(args.args))
        arguments.update(_pair(i, "--input") for i in args.input)
    except (catalog.CatalogError, ArtifactError, OSError, ValueError) as exc:
        print(f"cua catalog invoke: {exc}", file=sys.stderr)
        return EX_USAGE

    inputs, problems = catalog.coerce(cap, arguments)
    if problems:
        failure = catalog.input_invalid(
            cap, problems, args.idempotency_key or request.get("idempotency_key")
        )
        print(to_json(failure), end="")
        return int(exit_code(failure))
    return _invoke(
        "catalog invoke",
        args,
        path,
        tenant_id=args.tenant or request.get("tenant") or "local",
        inputs=inputs,
        request=request,
    )


def _print_result(command: str, result: Any) -> int:
    from cua.replay.result import exit_code, to_json

    if result.evidence.run_dir:
        run_dir = Path(result.evidence.run_dir)
        print(f"cua {command}: {result.kind} -> {link(run_dir)}", file=sys.stderr)
    if result.kind == "escalated":
        print(
            f"cua {command}: the session is still up. Once a person has handed back at "
            f"{link(result.operator_url or '')}, carry the run on with: "
            f"cua resume {result.resume_token}",
            file=sys.stderr,
        )
    print(to_json(result), end="")
    return int(exit_code(result))


def _resume(args: argparse.Namespace) -> int:
    from cua.replay.invocation import ApprovalGrant
    from cua.replay.runner import InvocationError, resume

    try:
        result = resume(
            args.resume_token,
            runs_dir=args.runs_dir,
            by=args.by,
            resume_at=args.resume_at,
            inputs=dict(_pair(i, "--input") for i in args.input),
            approval=ApprovalGrant(token=args.approval_token, approved_by=args.approved_by)
            if args.approval_token
            else None,
            wait_s=args.handoff_wait,
        )
    except (InvocationError, OSError, ValueError) as exc:
        print(f"cua resume: {exc}", file=sys.stderr)
        return EX_USAGE
    except KeyboardInterrupt:
        import logging

        logging.getLogger("asyncio").setLevel(logging.CRITICAL)
        print("cua resume: interrupted", file=sys.stderr)
        return 130
    return _print_result("resume", result)


def _operator(args: argparse.Namespace) -> int:
    import uvicorn

    from cua.escalation.operator_app import create_app
    from cua.policy.allowlist import load_policy
    from cua.tenant import load_tenant

    try:
        policy = load_policy(args.policy, load_tenant(args.tenant))
    except (OSError, ValueError) as exc:
        print(f"cua operator: {exc}", file=sys.stderr)
        return EX_USAGE
    print(
        f"cua operator: {link(f'http://{args.host}:{args.port}/')} (requests under "
        f"{link(args.runs_dir / '.interventions')})",
        file=sys.stderr,
    )
    uvicorn.run(
        create_app(args.runs_dir, policy=policy),
        host=args.host,
        port=args.port,
        log_level="warning",
    )
    return 0


def _pair(text: str, flag: str) -> tuple[str, str]:
    name, sep, value = text.partition("=")
    if not sep or not name.strip():
        raise ValueError(f"{flag} takes NAME=VALUE, got {text!r}")
    return name.strip(), value.strip()


def _record_run(
    run_dir: Path,
    out: Path,
    policy_path: Path,
    families_dir: Path = Path("capabilities/families"),
) -> None:
    """Record a run into ``out`` as a draft; raises ``RecordError``."""
    from cua.artifact.recorder import RecordError, record
    from cua.artifact.store import save
    from cua.policy.allowlist import load_policy
    from cua.tenant import Tenant

    try:
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        policy = load_policy(policy_path, Tenant.model_validate(run["tenant"]))
    except (OSError, KeyError, ValueError) as exc:
        raise RecordError(f"{run_dir.as_posix()} is not a readable discovery run: {exc}") from exc
    saved = save(record(run_dir, policy=policy, families_dir=families_dir), out)
    print(
        f"cua record: wrote {link(out)}, version {saved.version} ({saved.approval_state}). "
        f"Review it with: cua describe {out.as_posix()}",
        file=sys.stderr,
    )


def _record(args: argparse.Namespace) -> int:
    from cua.artifact.recorder import RecordError

    out = args.out
    if out is None:
        try:
            run = json.loads((args.run_dir / "run.json").read_text(encoding="utf-8"))
            out = Path("capabilities") / f"{run['goal']['name']}.json"
        except (OSError, KeyError, ValueError) as exc:
            print(
                f"cua record: {args.run_dir.as_posix()} is not a discovery run: {exc}",
                file=sys.stderr,
            )
            return EX_USAGE
    try:
        _record_run(args.run_dir, out, args.policy, args.families_dir)
    except RecordError as exc:
        print(f"cua record: {exc}", file=sys.stderr)
        return 1
    return 0


def _describe(args: argparse.Namespace) -> int:
    from cua.artifact.describe import describe
    from cua.artifact.store import ArtifactError, open_capability
    from cua.policy.approval import record_review

    try:
        loaded = open_capability(args.capability)
    except ArtifactError as exc:
        print(f"cua describe: {exc}", file=sys.stderr)
        return 1
    cap = loaded.capability
    if loaded.edited_outside:
        print(
            f"NOTE: this file was edited by hand after it was saved as version "
            f"{loaded.sealed_version}, so it is shown as version {cap.version}, a new draft.\n"
        )
    print(describe(cap), end="")
    review = record_review(cap, args.capability, state_dir=args.state_dir)
    if cap.approval_state == "draft":
        print(
            f"\nReviewed as version {review.version} (content {review.content_sha256[:12]}). "
            f"To approve exactly this: cua approve {args.capability.as_posix()} --by <your name>"
        )
    return 0


def _approve(args: argparse.Namespace) -> int:
    from cua.artifact.store import ArtifactError
    from cua.policy.approval import ApprovalRefused, approve

    try:
        cap = approve(args.capability, args.by, state_dir=args.state_dir)
    except (ApprovalRefused, ArtifactError) as exc:
        print(f"cua approve: refused: {exc}", file=sys.stderr)
        return 1
    print(
        f"cua approve: {cap.name} version {cap.version} is approved by {cap.approved_by} "
        f"({link(args.capability, stream=sys.stdout)})"
    )
    return 0


def _approval_token(args: argparse.Namespace) -> int:
    from cua.artifact.store import ArtifactError, open_capability
    from cua.policy import tokens
    from cua.replay.invocation import validate_inputs
    from cua.secrets.resolver import SecretError
    from cua.tenant import load_tenant

    try:
        tenant = load_tenant(args.tenant)
        cap = open_capability(args.capability).capability
        inputs = dict(_pair(i, "--input") for i in args.input)
        problems = validate_inputs(cap, inputs)
        if problems:
            raise ValueError("; ".join(problems))
        created = tokens.create_signing_key(tenant)
        if created is not None:
            print(f"cua approval-token: created a signing key at {link(created)}", file=sys.stderr)
        token = tokens.mint(
            cap,
            tenant,
            inputs,
            approved_by=args.by,
            key=tokens.signing_key(tenant),
            ttl_s=args.ttl_s,
        )
    except (ArtifactError, SecretError, tokens.TokenRefused, OSError, ValueError) as exc:
        print(f"cua approval-token: {exc}", file=sys.stderr)
        return 1
    print(
        f"cua approval-token: consent by {args.by.strip()} for {cap.name} v{cap.version} on "
        f"tenant {tenant.id}, these inputs only, one commit, {args.ttl_s} s",
        file=sys.stderr,
    )
    print(token)
    return 0


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
