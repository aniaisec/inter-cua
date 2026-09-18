"""One invocation, end to end: everything around the engine.

In order, and each check before anything more expensive:

1. load the capability (a hand-edited file loads as a new draft);
2. **approval gate** — unattended replay runs only an ``approved`` capability.
   A draft is ``POLICY_BLOCKED`` unless the operator overrides it explicitly;
3. **input shape** — ``INPUT_INVALID`` with no browser started;
4. **approval token**, if one came — signature, expiry, and that it covers
   this capability, content, tenant and inputs. Any mismatch is
   ``POLICY_BLOCKED``: consent for something else is not consent;
5. **idempotency** — the same key and request returns the stored result;
6. **spent token** — a token that already went into a commit is refused, so
   one consent is one commit (a caller retrying after a lost answer retries
   with its idempotency key, and step 5 answers it);
7. credentials resolved from the tenant binding (held in memory only);
8. a fresh browser session, traced; the engine runs; a failed run keeps its
   trace, scrubbed of secrets. If the run reached a risky step, its token is
   spent, whatever the result.

Returns a ``ReplayResult`` for every outcome a caller can act on. Raises
``InvocationError`` only for mistakes in how it was called — a missing file, an
unresolvable secret — which no result kind describes honestly.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote, quote_plus

from cua.artifact.schema import Capability
from cua.artifact.store import ArtifactError, open_capability
from cua.evidence import trace
from cua.evidence.logger import RUNS_DIR, RunLog, utc_now
from cua.policy import tokens
from cua.policy.allowlist import Policy
from cua.policy.redaction import Redactor
from cua.policy.tokens import Approval, SpentTokens, TokenRefused
from cua.replay.engine import ReplayConfig, ReplayEngine
from cua.replay.invocation import Invocation, request_summary, validate_inputs
from cua.replay.result import (
    Failure,
    IdempotencyCache,
    IdempotencyConflict,
    ReplayResult,
    fingerprint,
)
from cua.secrets.resolver import Credential, SecretError, resolve
from cua.surface.playwright_surface import PlaywrightSurface
from cua.tenant import Tenant

SurfaceFactory = Callable[[], AbstractContextManager[PlaywrightSurface]]


class InvocationError(Exception):
    """The request could not be made at all (not a replay result)."""


@contextmanager
def launched(headed: bool | None = None) -> Iterator[PlaywrightSurface]:
    with PlaywrightSurface.launch(headed=headed) as surface:
        yield surface


def replay(
    path: Path,
    *,
    tenant: Tenant,
    policy: Policy,
    invocation: Invocation,
    runs_dir: Path = RUNS_DIR,
    allow_draft: bool = False,
    config: ReplayConfig | None = None,
    surface: SurfaceFactory | None = None,
    environ: dict[str, str] | None = None,
) -> ReplayResult:
    try:
        loaded = open_capability(path)
    except ArtifactError as exc:
        raise InvocationError(str(exc)) from None
    cap = loaded.capability

    if cap.approval_state != "approved" and not allow_draft:
        edited = " (it was edited by hand after it was saved)" if loaded.edited_outside else ""
        return _refused(
            cap,
            invocation,
            "POLICY_BLOCKED",
            f"{cap.name} v{cap.version} is a draft{edited}; unattended replay runs only an "
            f"approved capability. Review it with `cua describe {path.as_posix()}`, then "
            "`cua approve`.",
        )
    if cap.target.app_family != tenant.app_family:
        return _refused(
            cap,
            invocation,
            "POLICY_BLOCKED",
            f"{cap.name} is for app family {cap.target.app_family!r}; tenant {tenant.id!r} "
            f"runs {tenant.app_family!r}",
        )

    problems = validate_inputs(cap, invocation.inputs)
    if problems:
        return _refused(cap, invocation, "INPUT_INVALID", "; ".join(problems))

    approval: Approval | None = None
    if invocation.approval is not None:
        try:
            approval = _verified(cap, tenant, invocation, environ)
        except TokenRefused as exc:
            return _refused(cap, invocation, "POLICY_BLOCKED", f"approval refused: {exc}")

    cache = IdempotencyCache(runs_dir) if invocation.idempotency_key else None
    request = fingerprint(cap.name, cap.version, invocation.inputs)
    if cache is not None and invocation.idempotency_key is not None:
        try:
            cached = cache.get(invocation.idempotency_key, request)
        except IdempotencyConflict as exc:
            return _refused(cap, invocation, "INPUT_INVALID", str(exc))
        if cached is not None:
            return cached

    spent = SpentTokens(runs_dir)
    if approval is not None:
        used_by = spent.spent_by(approval)
        if used_by is not None:
            return _refused(
                cap,
                invocation,
                "POLICY_BLOCKED",
                f"approval refused: this token was already used by {used_by}; one consent "
                "covers one commit. Retry with the same --idempotency-key to get that run's "
                "result, or ask for new consent.",
            )

    credentials = _credentials(cap, tenant, environ)
    result = _run(
        cap,
        tenant=tenant,
        policy=policy,
        invocation=invocation,
        approval=approval,
        spent=spent,
        credentials=credentials,
        runs_dir=runs_dir,
        allow_draft=allow_draft,
        config=config,
        surface=surface or launched,
    )
    if cache is not None and invocation.idempotency_key is not None:
        cache.put(invocation.idempotency_key, request, result)
    return result


def _run(
    cap: Capability,
    *,
    tenant: Tenant,
    policy: Policy,
    invocation: Invocation,
    approval: Approval | None,
    spent: SpentTokens,
    credentials: dict[str, Credential],
    runs_dir: Path,
    allow_draft: bool,
    config: ReplayConfig | None,
    surface: SurfaceFactory,
) -> ReplayResult:
    log = RunLog.create(runs_dir)
    log.write_json(
        "run.json",
        {
            "run_id": log.run_id,
            "kind": "replay",
            "started_at": utc_now(),
            "capability": {
                "id": cap.id,
                "name": cap.name,
                "version": cap.version,
                "approval_state": cap.approval_state,
                "approved_by": cap.approved_by,
                "content_sha256": cap.content_hash(),
            },
            "tenant": {
                "id": tenant.id,
                "app_family": tenant.app_family,
                "base_url": tenant.base_url,
            },
            "request": request_summary(invocation, cap),
            "allow_draft": allow_draft,
        },
    )
    if allow_draft and cap.approval_state != "approved":
        log.event("policy.draft_override", warning="a draft capability was replayed by override")
        print(
            f"WARNING: replaying draft {cap.name} v{cap.version} by --allow-draft override",
            file=sys.stderr,
        )

    secrets = [v for c in credentials.values() for v in c.values()]
    secrets += [v for n, v in invocation.inputs.items() if cap.inputs[n].sensitive]
    with surface() as live:
        context = live.page.context
        trace.start(context)
        engine = ReplayEngine(
            capability=cap,
            surface=live,
            tenant=tenant,
            policy=policy,
            invocation=invocation,
            credentials=credentials,
            log=log,
            approval=approval,
            config=config,
        )
        # An interruption (Ctrl+C, a crash) is written by the engine as a
        # ``Failure INTERRUPTED`` with the side effect it can vouch for, and
        # then re-raised; the browser is closed on the way out.
        try:
            result = engine.run()
        finally:
            if approval is not None and engine.side_effect_so_far() != "none":
                spent.spend(approval, log.run_id)
                log.event("approval.spent", token_sha256=approval.token_sha256[:12])
        keep = log.dir / trace.TRACE_NAME if result.kind == "failure" else None
        kept = trace.stop(
            context, keep_as=keep, redactor=Redactor.for_policy(policy, _encodings(secrets))
        )

    if kept is not None:
        evidence = result.evidence.model_copy(
            update={"trace": kept.relative_to(log.dir).as_posix()}
        )
        result = result.model_copy(update={"evidence": evidence})
        log.write_json("result.json", result)
    return result


def _verified(
    cap: Capability, tenant: Tenant, invocation: Invocation, environ: dict[str, str] | None
) -> Approval:
    assert invocation.approval is not None
    grant = invocation.approval
    approval = tokens.verify(
        grant.token,
        cap,
        tenant,
        invocation.inputs,
        key=tokens.signing_key(tenant, environ=environ),
    )
    if grant.approved_by is not None and grant.approved_by != approval.approved_by:
        raise TokenRefused(
            f"the token was signed for {approval.approved_by!r}, not {grant.approved_by!r}"
        )
    return approval


def _credentials(
    cap: Capability, tenant: Tenant, environ: dict[str, str] | None
) -> dict[str, Credential]:
    out: dict[str, Credential] = {}
    for name, spec in cap.credentials.items():
        ref = spec.ref.replace("{tenant.id}", tenant.id)
        try:
            credential = resolve(ref, tenant, environ=environ)
        except SecretError as exc:
            raise InvocationError(f"credential {name}: {exc}") from None
        missing = [f for f in spec.fields if f not in credential.field_names]
        if missing:
            raise InvocationError(f"credential {name} ({ref}) has no field(s) {missing}")
        out[name] = credential
    return out


def _encodings(secrets: list[str]) -> list[str]:
    """Each secret as it may appear inside a trace: raw, form-encoded, URL
    encoded, and JSON-escaped."""
    out: set[str] = set()
    for s in secrets:
        if s:
            out.update({s, quote_plus(s), quote(s, safe=""), json.dumps(s)[1:-1]})
    return sorted(out)


def _refused(
    cap: Capability,
    invocation: Invocation,
    code: Literal["POLICY_BLOCKED", "INPUT_INVALID"],
    message: str,
) -> Failure:
    """Turned away before any browser started: certainly no side effect."""
    return Failure(
        code=code,
        message=message,
        side_effect="none",
        capability=cap.name,
        capability_version=cap.version,
        idempotency_key=invocation.idempotency_key,
    )
