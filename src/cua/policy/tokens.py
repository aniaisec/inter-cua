"""Approval tokens: consent a caller already holds, for one exact invocation.

A step that commits something (``approval: required``, or a control the
policy's risky rules match) runs unattended only with an approval. A token is
how a calling agent brings one: someone entitled to consent signs *this*
capability, at *this* content, on *this* tenant, with *these* inputs, until a
deadline — and the token says so in a form the runner can check without
trusting the caller.

Checked before the browser starts. A token that fails any check is
``POLICY_BLOCKED`` with nothing touched; an absent token is not a failure of
its own, it just leaves the risky step to a human.

Format: ``cat1.<claims>.<mac>`` — base64url JSON claims and an HMAC-SHA256 over
them with the tenant's signing key (``secret://<tenant>/cua/approval-signing-key``).
The claims are readable by design; they carry no input values, only a hash of
them, and the token itself is only ever logged as a hash prefix.

One token, one commit: once a run has performed the risky step (or may have),
the token is spent. A caller retrying after a lost answer should retry with the
same idempotency key, which returns the first result instead of committing
again; a new commit needs new consent.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from cua.artifact.schema import Capability
from cua.secrets.resolver import SecretError, binding_for, resolve
from cua.tenant import Tenant

PREFIX = "cat1"
SIGNING_KEY = "cua/approval-signing-key"
MIN_KEY_CHARS = 32
DEFAULT_TTL_S = 15 * 60
MAX_TTL_S = 24 * 60 * 60


class TokenRefused(Exception):
    """The token does not grant what this invocation needs, and why."""


class Approval(BaseModel):
    """Consent that has been checked: the only thing that satisfies a risky step."""

    model_config = ConfigDict(frozen=True)

    capability: str
    version: int
    tenant: str
    approved_by: str
    expires_at: int
    token_sha256: str
    via: Literal["token", "console"] = "token"
    """``console``: given on the operator console in answer to an
    intervention request, for that request's step only. There is no token
    then, and ``token_sha256`` is a hash of the request id; nothing is spent."""


class _Claims(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cap: str
    ver: int
    content: str
    """Content hash prefix: an edited capability is a different thing to consent to."""
    tenant: str
    inputs: str
    """SHA-256 of the canonical inputs; never the values."""
    by: str
    iat: int
    exp: int
    nonce: str


def signing_key_ref(tenant: Tenant) -> str:
    return f"secret://{tenant.id}/{SIGNING_KEY}"


def signing_key(
    tenant: Tenant, *, environ: dict[str, str] | None = None, root: Path | None = None
) -> bytes:
    """The tenant's signing key, resolved like any other secret."""
    try:
        value = resolve(signing_key_ref(tenant), tenant, environ=environ, root=root).field("value")
    except SecretError as exc:
        raise TokenRefused(f"no approval signing key: {exc}") from None
    if len(value) < MIN_KEY_CHARS:
        raise TokenRefused(
            f"the approval signing key is shorter than {MIN_KEY_CHARS} characters; "
            "a guessable key makes every token forgeable"
        )
    return value.encode("utf-8")


def create_signing_key(tenant: Tenant, *, root: Path | None = None) -> Path | None:
    """Write a fresh random key where the tenant's file binding expects one.

    Only for a file binding whose file does not exist yet; returns the path it
    wrote, or None when there was nothing to do (an env binding is the
    operator's to set, and an existing key is never replaced — that would
    invalidate every token already issued).
    """
    binding = binding_for(signing_key_ref(tenant), tenant)
    if binding.provider != "file" or binding.path is None:
        return None
    path = (root or Path.cwd()) / binding.path
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    return path


def inputs_digest(inputs: dict[str, str]) -> str:
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint(
    capability: Capability,
    tenant: Tenant,
    inputs: dict[str, str],
    *,
    approved_by: str,
    key: bytes,
    ttl_s: int = DEFAULT_TTL_S,
    now: float | None = None,
) -> str:
    """Sign consent for one invocation of one capability."""
    approved_by = approved_by.strip()
    if not approved_by:
        raise TokenRefused("say who is giving consent")
    if not 0 < ttl_s <= MAX_TTL_S:
        raise TokenRefused(f"a token lives between 1 s and {MAX_TTL_S} s, not {ttl_s}")
    issued = int(time.time() if now is None else now)
    claims = _Claims(
        cap=capability.name,
        ver=capability.version,
        content=capability.content_hash()[:16],
        tenant=tenant.id,
        inputs=inputs_digest(inputs),
        by=approved_by,
        iat=issued,
        exp=issued + ttl_s,
        nonce=secrets.token_hex(8),
    )
    body = _b64(claims.model_dump_json().encode("utf-8"))
    return f"{PREFIX}.{body}.{_b64(_mac(key, body))}"


def verify(
    token: str,
    capability: Capability,
    tenant: Tenant,
    inputs: dict[str, str],
    *,
    key: bytes,
    now: float | None = None,
) -> Approval:
    """The consent this token carries, if it covers exactly this invocation."""
    prefix, _, rest = token.strip().partition(".")
    body, _, mac = rest.partition(".")
    if prefix != PREFIX or not body or not mac:
        raise TokenRefused("not an approval token (expected cat1.<claims>.<signature>)")
    try:
        signature = _unb64(mac)
    except ValueError:
        raise TokenRefused("the token's signature is not readable") from None
    if not hmac.compare_digest(signature, _mac(key, body)):
        raise TokenRefused(
            f"the token's signature does not verify with tenant {tenant.id!r}'s key "
            "(altered, or signed for another tenant)"
        )
    try:
        claims = _Claims.model_validate_json(_unb64(body))
    except (ValueError, ValidationError):
        raise TokenRefused("the token's claims are not readable") from None

    at = int(time.time() if now is None else now)
    if at >= claims.exp:
        raise TokenRefused(f"the token expired {at - claims.exp} s ago; ask for new consent")
    if claims.iat > at + 60:
        raise TokenRefused("the token was issued in the future; check the clocks")
    mismatches = [
        what
        for what, ok in (
            (f"capability {claims.cap!r}", claims.cap == capability.name),
            (f"version {claims.ver}", claims.ver == capability.version),
            ("that content", claims.content == capability.content_hash()[:16]),
            (f"tenant {claims.tenant!r}", claims.tenant == tenant.id),
            ("other inputs", claims.inputs == inputs_digest(inputs)),
        )
        if not ok
    ]
    if mismatches:
        raise TokenRefused(
            f"the token grants consent for {', '.join(mismatches)}, not for this invocation "
            f"of {capability.name} v{capability.version}"
        )
    return Approval(
        capability=claims.cap,
        version=claims.ver,
        tenant=claims.tenant,
        approved_by=claims.by,
        expires_at=claims.exp,
        token_sha256=token_sha256(token.strip()),
    )


class SpentTokens:
    """Tokens that have been used for a commit, one file each.

    A file rather than a store for the same reason as the idempotency cache:
    one process is the whole deployment here. Kept beside the runs, keyed by
    the token's hash, never the token.
    """

    def __init__(self, root: Path) -> None:
        self.dir = root / ".approvals-spent"

    def _path(self, approval: Approval) -> Path:
        return self.dir / f"{approval.token_sha256[:32]}.json"

    def spent_by(self, approval: Approval) -> str | None:
        """The run that used this token, if one did."""
        path = self._path(approval)
        if not path.is_file():
            return None
        return str(json.loads(path.read_text(encoding="utf-8")).get("run_id", "an earlier run"))

    def spend(self, approval: Approval, run_id: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        record = {"run_id": run_id, "capability": approval.capability, "at": int(time.time())}
        self._path(approval).write_text(json.dumps(record) + "\n", encoding="utf-8")


def _mac(key: bytes, body: str) -> bytes:
    return hmac.new(key, f"{PREFIX}.{body}".encode("ascii"), hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError(str(exc)) from None
