"""Who may call the API, for which tenants, and what for (``api/access.yaml``).

Every request but ``GET /health`` carries three things, and each is checked
before anything else happens:

* ``Authorization: Bearer <key>`` — which client is calling. A key is bound
  like any secret, to an environment variable or a file, never written in the
  access file itself; it is compared in constant time.
* ``X-Cua-Tenant: <id>`` — the tenant the call is for. A client names the
  tenants it serves; any other is refused. The tenant is never inferred:
  calling one tenant's capability on another's deployment is exactly the
  mistake a default would make silently.
* the client's ``scopes`` and ``capabilities``. ``read`` lists capabilities
  and reads runs; ``invoke`` starts runs; ``approve`` carries consent (a
  signed approval token) into a run; ``operate`` resumes and aborts escalated
  runs. ``capabilities`` names the capabilities a client may see and run
  (``"*"``: all of them).

The token is the consent and the scope is the permission to deliver it:
carrying an approval token needs ``approve`` whether it rides on a new run or
on ``POST /runs/{id}/approve``.
"""

from __future__ import annotations

import hmac
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cua.api.models import ApiError
from cua.policy.allowlist import Policy, load_policy
from cua.tenant import TENANTS_DIR, SecretBinding, Tenant, load_tenant, shared_bindings

ACCESS_FILE = Path("api/access.yaml")
MIN_KEY_CHARS = 32

Scope = Literal["read", "invoke", "approve", "operate"]


class TenantAccess(BaseModel):
    """A tenant this server serves, and the policy its runs are held to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: Path | None = None
    """Default: the tenant file's own (``policy:``)."""
    file: Path | None = None
    """The tenant file; default ``tenants/<id>.yaml``."""


class Client(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: SecretBinding
    tenants: list[str] = Field(min_length=1)
    capabilities: list[str] = Field(default_factory=list)
    scopes: list[Scope] = Field(default_factory=lambda: list[Scope](["read"]))

    def may_use(self, capability: str) -> bool:
        return "*" in self.capabilities or capability in self.capabilities


class AccessConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenants: dict[str, TenantAccess] = Field(default_factory=dict)
    clients: dict[str, Client] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _tenants_declared(self) -> AccessConfig:
        for name, client in self.clients.items():
            unknown = sorted(set(client.tenants) - set(self.tenants))
            if unknown:
                raise ValueError(f"client {name!r} names undeclared tenant(s) {unknown}")
        return self


def load_access(path: Path = ACCESS_FILE) -> AccessConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return AccessConfig.model_validate(data)


@dataclass(frozen=True)
class Caller:
    """An authenticated request: who, for which tenant, under which id."""

    client_id: str
    client: Client
    tenant: Tenant
    policy: Policy
    request_id: str

    def require(self, scope: Scope) -> None:
        if scope not in self.client.scopes:
            raise ApiError(
                403, "scope_missing", f"client {self.client_id!r} does not have scope {scope!r}"
            )

    def require_capability(self, name: str) -> None:
        if not self.client.may_use(name):
            raise ApiError(
                403,
                "capability_not_authorized",
                f"client {self.client_id!r} is not authorized for capability {name!r}",
            )


class Gate:
    """The access file, with every client's key read and every tenant loaded.

    A client whose key cannot be read (an unset variable, a missing file, a
    key too short to be unguessable) is disabled, never let in without one;
    ``disabled`` says why, for the server to report at startup. A tenant or
    policy that does not load is an error at startup, not at the first call,
    and so are two served tenants bound to one secret: isolation between them
    would be a claim their keys do not back (``cua.tenant.shared_bindings``).
    """

    def __init__(
        self,
        config: AccessConfig,
        *,
        root: Path | None = None,
        environ: dict[str, str] | None = None,
        tenants_dir: Path = TENANTS_DIR,
    ) -> None:
        self.config = config
        base = root or Path.cwd()
        env = os.environ if environ is None else environ
        self._keys: dict[str, bytes] = {}
        self.disabled: dict[str, str] = {}
        for name, client in config.clients.items():
            key, why = _read_key(client.key, base, env)
            if key is None:
                self.disabled[name] = why
            else:
                self._keys[name] = key
        self.tenants: dict[str, tuple[Tenant, Policy]] = {}
        for tenant_id, spec in config.tenants.items():
            tenant = load_tenant(
                str(spec.file) if spec.file is not None else tenant_id, root=tenants_dir
            )
            if tenant.id != tenant_id:
                raise ValueError(f"tenant {tenant_id!r} loads a file for tenant {tenant.id!r}")
            self.tenants[tenant_id] = (
                tenant,
                load_policy(spec.policy or tenant.policy_file, tenant),
            )
        shared = shared_bindings([tenant for tenant, _ in self.tenants.values()])
        if shared:
            raise ValueError("; ".join(shared))

    def authenticate(self, authorization: str | None) -> tuple[str, Client]:
        scheme, _, presented = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not presented.strip():
            raise ApiError(401, "unauthenticated", "send Authorization: Bearer <api key>")
        given = presented.strip().encode("utf-8")
        found: str | None = None
        for name, key in self._keys.items():
            # Every key is compared, so the time taken says nothing about which
            # client (if any) a guess came close to.
            if hmac.compare_digest(given, key) and found is None:
                found = name
        if found is None:
            raise ApiError(401, "unauthenticated", "the API key is not valid")
        return found, self.config.clients[found]

    def caller(self, authorization: str | None, tenant_id: str | None, request_id: str) -> Caller:
        client_id, client = self.authenticate(authorization)
        if not tenant_id:
            raise ApiError(400, "tenant_missing", "name the tenant: X-Cua-Tenant: <id>")
        if tenant_id not in client.tenants or tenant_id not in self.tenants:
            raise ApiError(
                403,
                "tenant_not_authorized",
                f"client {client_id!r} is not authorized for tenant {tenant_id!r}",
            )
        tenant, policy = self.tenants[tenant_id]
        return Caller(client_id, client, tenant, policy, request_id)


def create_keys(config: AccessConfig, *, root: Path | None = None) -> list[Path]:
    """A fresh random key for every client bound to a file that does not
    exist yet; the paths written. An existing key is never replaced, and an
    environment binding is the operator's to set."""
    base = root or Path.cwd()
    written = []
    for client in config.clients.values():
        binding = client.key
        if binding.provider != "file" or binding.path is None:
            continue
        path = base / binding.path
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8", newline="\n")
        written.append(path)
    return written


def _read_key(
    binding: SecretBinding, root: Path, environ: os._Environ[str] | dict[str, str]
) -> tuple[bytes | None, str]:
    if binding.provider == "env":
        value = environ.get(binding.var or "", "")
    else:
        path = root / (binding.path or "")
        try:
            value = path.read_text(encoding="utf-8")
        except OSError:
            return None, f"no key at {binding.source}"
    value = value.strip()
    if not value:
        return None, f"no key at {binding.source}"
    if len(value) < MIN_KEY_CHARS:
        return None, f"the key at {binding.source} is shorter than {MIN_KEY_CHARS} characters"
    return value.encode("utf-8"), ""
