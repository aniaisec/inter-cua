"""``cua workflow list | check | run | approval-token``.

``list`` and ``check`` read workflows and the registry and write nothing.
``run`` runs one through replay, step by step, and prints a JSON
WorkflowResult (exit 0 success, 2 business outcome, 1 failure, 3 escalated,
like ``cua replay``). ``approval-token`` mints consent for one committing
step, for the inputs that step will get. Imports no model client.
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
    from cua.cli import _add_handoff_flags

    w = sub.add_parser(
        "workflow",
        help="Compose approved capabilities into one typed request",
        description="A workflow (workflows/<name>.yaml) runs approved capabilities one after "
        "another, wiring its inputs and earlier steps' outputs into later steps' inputs. "
        "Every reference and type is checked before anything runs, and every step runs "
        "through replay with its own approval, consent, idempotency and budget.",
    )
    wsub = w.add_subparsers(dest="workflow_command", required=True)

    def dirs(p: argparse.ArgumentParser) -> None:
        p.add_argument("--workflows-dir", type=Path, default=Path("workflows"))
        p.add_argument("--capabilities-dir", type=Path, default=Path("capabilities"))

    ls = wsub.add_parser("list", help="The workflows, and whether each can run")
    ls.add_argument("--tenant", default="local", help="tenants/<id>.yaml, or a path to one")
    dirs(ls)

    c = wsub.add_parser(
        "check", help="The plan: resolved versions, bindings, types, and every problem"
    )
    c.add_argument("workflow", help="A name in workflows/, or a path")
    c.add_argument("--tenant", default="local", help="tenants/<id>.yaml, or a path to one")
    c.add_argument("--json", action="store_true", help="Machine-readable output")
    dirs(c)

    r = wsub.add_parser(
        "run",
        help="Run a workflow; prints a JSON WorkflowResult",
        description="Run a workflow step by step through replay. Exit 0 success, "
        "2 business outcome, 1 failure, 3 escalated.",
    )
    r.add_argument("workflow", help="A name in workflows/, or a path")
    r.add_argument("--tenant", default="local", help="tenants/<id>.yaml, or a path to one")
    r.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    r.add_argument(
        "--idempotency-key",
        help="Required when a step commits. The same key and inputs again returns the "
        "stored result, or carries on after a step a person has since finished",
    )
    r.add_argument(
        "--approval",
        action="append",
        default=[],
        metavar="STEP=TOKEN",
        help="Consent for one step's commit (from `cua workflow approval-token`)",
    )
    r.add_argument("--policy", default="policies/default.yaml", type=Path)
    r.add_argument(
        "--budget",
        default="",
        metavar="timeout_s=300,max_recoveries=3,allow_escalation=true",
        help="timeout_s is for the whole workflow; the rest apply to each step",
    )
    r.add_argument("--step-timeout", type=float, default=3.0, help="Seconds one step may take")
    r.add_argument(
        "--inject",
        action="append",
        default=[],
        metavar="STEP=MODE",
        help="Arm a mock-app failure mode for one step (demo)",
    )
    r.add_argument("--no-screenshots", action="store_true")
    r.add_argument("--headed", action="store_true", help="Show the browser window")
    r.add_argument("--runs-dir", type=Path, default=Path("evidence/runs"))
    dirs(r)
    _add_handoff_flags(r)

    t = wsub.add_parser(
        "approval-token",
        help="Consent for one committing step of a workflow, for these inputs only",
        description="Mint an approval token for one step: its capability, version and "
        "content, this tenant, and the inputs the step will get from these workflow inputs. "
        "Prints the token on stdout.",
    )
    t.add_argument("workflow", help="A name in workflows/, or a path")
    t.add_argument("--step", required=True, help="The committing step")
    t.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    t.add_argument("--tenant", default="local", help="tenants/<id>.yaml, or a path to one")
    t.add_argument("--by", required=True, help="Who is giving consent")
    t.add_argument("--ttl-s", type=int, default=900, help="Seconds the token is valid (900)")
    dirs(t)


def main(args: argparse.Namespace) -> int:
    from pydantic import ValidationError

    from cua.registry.store import RegistryError
    from cua.workflow.models import WorkflowError

    command = args.workflow_command
    try:
        if command == "list":
            return _list(args)
        if command == "check":
            return _check(args)
        if command == "run":
            return _run(args)
        return _approval_token(args)
    except (WorkflowError, RegistryError, OSError, ValueError, ValidationError) as exc:
        print(f"cua workflow {command}: {exc}", file=sys.stderr)
        return EX_USAGE


def _plan(args: argparse.Namespace, name: str) -> Any:
    from cua.registry.store import Registry
    from cua.workflow.models import find_workflow, load_workflow
    from cua.workflow.planner import plan

    path = find_workflow(name, args.workflows_dir)
    return path, plan(load_workflow(path), Registry(args.capabilities_dir))


def _list(args: argparse.Namespace) -> int:
    from cua.tenant import load_tenant
    from cua.workflow import validator
    from cua.workflow.models import WorkflowError

    paths = sorted(args.workflows_dir.glob("*.yaml")) if args.workflows_dir.is_dir() else []
    if not paths:
        print(f"No workflows in {args.workflows_dir.as_posix()}.")
        return 0
    tenant = load_tenant(args.tenant)
    for path in paths:
        try:
            _, p = _plan(args, path.as_posix())
        except WorkflowError as exc:
            print(f"{path.stem}  cannot be planned: {exc}")
            continue
        wf = p.workflow
        issues = validator.problems(p) + validator.refusals(p, tenant)
        state = "ready" if not issues else f"{len(issues)} problem(s): cua workflow check {wf.name}"
        print(f"{wf.name}  v{wf.version}  {state}")
        if wf.description:
            print(f"    {wf.description}")
        chain = " -> ".join(
            f"{s.id}: {s.capability.name} v{s.version}{' (commits)' if s.needs_consent else ''}"
            for s in p.steps
        )
        print(f"    {chain}")
        print(f"    {link(path, stream=sys.stdout)}")
    return 0


def _check(args: argparse.Namespace) -> int:
    from cua.tenant import load_tenant
    from cua.workflow import validator

    path, p = _plan(args, args.workflow)
    tenant = load_tenant(args.tenant)
    wf = p.workflow
    problems = validator.problems(p)
    refusals = validator.refusals(p, tenant)
    outputs = validator.output_types(p) if not problems else {}
    if args.json:
        print(
            json.dumps(
                {
                    "workflow": wf.name,
                    "version": wf.version,
                    "content_sha256": wf.content_hash(),
                    "path": path.as_posix(),
                    "steps": [
                        {
                            "id": s.id,
                            "capability": s.capability.name,
                            "version": s.version,
                            "status": s.status,
                            "needs_consent": s.needs_consent,
                            "inputs": s.spec.inputs,
                        }
                        for s in p.steps
                    ],
                    "outputs": {n: {"type": t, "optional": o} for n, (t, o) in outputs.items()},
                    "problems": problems,
                    "refusals": refusals,
                },
                indent=2,
            )
        )
        return 0 if not (problems or refusals) else 1

    print(f"{wf.name} v{wf.version}  ({link(path, stream=sys.stdout)})")
    if wf.description:
        print(f"    {wf.description}")
    ins = ", ".join(
        f"{n}: {s.type}{'' if s.required else '?'}{' (sensitive)' if s.sensitive else ''}"
        for n, s in wf.inputs.items()
    )
    print(f"inputs: {ins or 'none'}")
    for i, s in enumerate(p.steps, 1):
        cap = s.capability
        consent = "  commits: needs consent" if s.needs_consent else ""
        print(f"{i}. {s.id}: {cap.name} v{s.version} ({s.status}){consent}")
        for name, text in s.spec.inputs.items():
            spec = cap.inputs.get(name)
            typed = f" ({spec.type})" if spec else ""
            print(f"     {name}{typed} <- {text}")
        outs = ", ".join(
            f"{n}: {o.type}{'?' if o.optional else ''}" for n, o in cap.outputs.items()
        )
        print(f"     -> {outs or 'no outputs'}")
    if outputs:
        print(
            "outputs: " + ", ".join(f"{n}: {t}{'?' if o else ''}" for n, (t, o) in outputs.items())
        )
    for problem in problems:
        print(f"PROBLEM: {problem}")
    for refusal in refusals:
        print(f"REFUSED on tenant {tenant.id}: {refusal}")
    if problems or refusals:
        return 1
    print("OK: every reference and type checks, and every step may run on this tenant.")
    return 0


def _run(args: argparse.Namespace) -> int:
    from cua.cli import _handoff_settings, _pair
    from cua.policy.allowlist import load_policy
    from cua.replay.engine import ReplayConfig
    from cua.replay.invocation import ApprovalGrant, Budget
    from cua.replay.result import EXIT_CODES
    from cua.replay.runner import InvocationError, launched
    from cua.tenant import load_tenant
    from cua.workflow.models import find_workflow, to_json
    from cua.workflow.runner import WorkflowRequest, run

    try:
        path = find_workflow(args.workflow, args.workflows_dir)
        tenant = load_tenant(args.tenant)
        policy = load_policy(args.policy, tenant)
        handoff = _handoff_settings(args)
        request = WorkflowRequest(
            inputs=dict(_pair(i, "--input") for i in args.input),
            idempotency_key=args.idempotency_key,
            approvals={
                step: ApprovalGrant(token=token)
                for step, token in (_pair(a, "--approval") for a in args.approval)
            },
            budget=Budget.model_validate(
                {
                    "timeout_s": 300.0,
                    **dict(_pair(b, "--budget") for b in args.budget.split(",") if b.strip()),
                }
            ),
            inject=dict(_pair(i, "--inject") for i in args.inject),
        )
        result = run(
            path,
            tenant=tenant,
            policy=policy,
            request=request,
            capabilities_dir=args.capabilities_dir,
            runs_dir=args.runs_dir,
            config=ReplayConfig(
                step_timeout_s=args.step_timeout, screenshots=not args.no_screenshots
            ),
            surface=lambda: launched(headed=args.headed or None, detached=handoff is not None),
            handoff=handoff,
        )
    except InvocationError as exc:
        print(f"cua workflow run: {exc}", file=sys.stderr)
        return EX_USAGE
    except KeyboardInterrupt:
        print("cua workflow run: interrupted", file=sys.stderr)
        return 130

    for step in result.steps:
        if step.run_dir:
            cached = " (cached)" if step.cached else ""
            print(
                f"cua workflow run: {step.id}: {step.kind}{cached} -> {link(Path(step.run_dir))}",
                file=sys.stderr,
            )
    if result.run_dir:
        print(f"cua workflow run: {result.kind} -> {link(Path(result.run_dir))}", file=sys.stderr)
    if result.kind == "escalated":
        print(
            f"cua workflow run: step {result.step_id} waits for a person. Once they hand back "
            f"at {link(result.operator_url or '')}, finish it with: cua resume "
            f"{result.resume_token} -- then run the workflow again with the same "
            "--idempotency-key to carry on.",
            file=sys.stderr,
        )
    print(to_json(result), end="")
    return EXIT_CODES[result.kind]


def _approval_token(args: argparse.Namespace) -> int:
    from cua.cli import _pair
    from cua.policy import tokens
    from cua.tenant import load_tenant
    from cua.workflow import validator
    from cua.workflow.planner import static_inputs

    path, p = _plan(args, args.workflow)
    problems = validator.problems(p)
    if problems:
        raise ValueError(f"{path.as_posix()}: " + "; ".join(problems))
    try:
        step = p.step(args.step)
    except KeyError:
        raise ValueError(
            f"no step {args.step!r} (steps: {', '.join(s.id for s in p.steps)})"
        ) from None
    if not step.needs_consent:
        raise ValueError(f"step {step.id!r} commits nothing; it takes no approval")
    inputs = static_inputs(step, dict(_pair(i, "--input") for i in args.input))
    if inputs is None:
        raise ValueError(
            f"step {step.id!r} takes inputs from an earlier step's outputs, which are not "
            "known until the workflow runs; run it with --handoff for a person to consent "
            "to the values it reads"
        )
    from cua.replay.invocation import validate_inputs

    wrong = validate_inputs(step.capability, inputs)
    if wrong:
        raise ValueError(f"step {step.id!r}: " + "; ".join(wrong))
    tenant = load_tenant(args.tenant)
    try:
        created = tokens.create_signing_key(tenant)
        if created is not None:
            print(
                f"cua workflow approval-token: created a signing key at {link(created)}",
                file=sys.stderr,
            )
        token = tokens.mint(
            step.capability,
            tenant,
            inputs,
            approved_by=args.by,
            key=tokens.signing_key(tenant),
            ttl_s=args.ttl_s,
        )
    except tokens.TokenRefused as exc:
        print(f"cua workflow approval-token: {exc}", file=sys.stderr)
        return 1
    cap = step.capability
    print(
        f"cua workflow approval-token: consent by {args.by.strip()} for step {step.id} "
        f"({cap.name} v{cap.version}) of {p.workflow.name} on tenant {tenant.id}, these "
        f"inputs only, one commit, {args.ttl_s} s",
        file=sys.stderr,
    )
    print(token)
    return 0
