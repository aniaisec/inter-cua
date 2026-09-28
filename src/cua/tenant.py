"""Tenant binding: the per-deployment facts a capability must not carry.

A capability is written against an application family ("legacy-core"), not
against one credit union's server. ``tenants/<id>.yaml`` says where that app
lives for one tenant and where its credentials come from, so the same
capability runs anywhere the family is deployed.

Isolation between tenants rests on this file. Every invocation resolves, from
the tenant and nothing else: the deployment it acts on (``base_url``), the
credentials it may use (``secrets``; a ``secret://<tenant>/...`` reference
resolves only for its own tenant), the policy it is held to (``policy``), the
capabilities it may run (``app_family``, narrowed by ``capabilities``) and how
a shared capability is adapted to it (``overlay``). Idempotency records,
evidence and approval tokens are keyed by the tenant's id, so no record made
for one tenant answers a request for another.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

TENANTS_DIR = Path("tenants")
DEFAULT_POLICY = Path("policies/default.yaml")
SYSTEM_SECRET_PREFIX = "cua/"


class SecretBinding(BaseModel):
    """Where one secret's value comes from. Never the value itself.

    ``env``: an environment variable (``var``). ``file``: a file (``path``,
    relative to the working directory), for a value a secret manager mounts
    or one the operator keeps outside the shell's environment.
    """

    model_config = ConfigDict(frozen=True)

    provider: Literal["env", "file"] = "env"
    var: str | None = None
    path: str | None = None
    format: str = "value"
    """How the raw value splits into fields: ``username:password`` means the
    text before the first colon is ``username`` and the rest is ``password``."""

    @model_validator(mode="after")
    def _source_named(self) -> SecretBinding:
        if self.provider == "env" and not self.var:
            raise ValueError("an env secret binding names its variable (var)")
        if self.provider == "file" and not self.path:
            raise ValueError("a file secret binding names its file (path)")
        return self

    @property
    def fields(self) -> list[str]:
        return self.format.split(":")

    @property
    def source(self) -> str:
        """Where the value is read from, for a message that must not show it."""
        return f"${self.var}" if self.provider == "env" else f"file {self.path}"


class DesktopApp(BaseModel):
    """How this tenant's desktop application is started.

    Tenant configuration, like ``base_url``: a capability names an application
    family and a ``uia://`` location, never a command, so a reviewed artifact
    cannot be made to start an arbitrary program. Paths are relative to the
    working directory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    launch: list[str] = Field(min_length=1)
    inject_flag: str | None = None
    """How a test deployment is told which fault to inject (``?inject=x`` on
    the entry location). None: this deployment takes no injections."""


class Tenant(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    app_family: str
    base_url: str
    """``http(s)://`` for a web deployment; ``uia://<app>`` for a desktop one,
    which also needs ``desktop``."""
    secrets: dict[str, SecretBinding] = Field(default_factory=dict)
    policy: str | None = None
    """The policy this tenant's runs are held to, relative to the working
    directory; default ``policies/default.yaml``. A caller may name another
    one explicitly (``--policy``)."""
    capabilities: list[str] | None = None
    """The capabilities this tenant runs, by name. None: every capability of
    its application family. A list narrows that: a capability written for one
    tenant's deployment of a shared product need not run on another's."""
    overlay: str | None = None
    """How a shared capability is adapted to this tenant's wording and layout.
    Not implemented by this runtime, so a tenant that names one is refused
    rather than run as if it had none."""
    desktop: DesktopApp | None = None

    @model_validator(mode="after")
    def _no_overlay(self) -> Tenant:
        if self.overlay is not None:
            raise ValueError(
                f"tenant {self.id!r} names overlay {self.overlay!r}, and this runtime applies "
                "no overlays; running its capabilities unadapted would drive a layout they "
                "were not written for"
            )
        return self

    @model_validator(mode="after")
    def _desktop_bound(self) -> Tenant:
        if self.base_url.startswith("uia://") and self.desktop is None:
            raise ValueError("a uia:// tenant says how its application starts (desktop.launch)")
        if self.desktop is not None and not self.base_url.startswith("uia://"):
            raise ValueError("a desktop tenant's base_url is its uia://<app> location")
        return self

    def url(self, path: str) -> str:
        """An absolute URL on this tenant's deployment."""
        if path.startswith(("http://", "https://")):
            return path
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    def refusal(self, capability: str, app_family: str) -> str | None:
        """Why this tenant does not run ``capability`` (of ``app_family``), or
        None if it does. The one check every entry point makes: replay, a
        workflow, the API, MCP and the registry's tenant scope."""
        if app_family != self.app_family:
            return (
                f"{capability} is for app family {app_family!r}; tenant {self.id!r} "
                f"runs {self.app_family!r}"
            )
        if self.capabilities is not None and capability not in self.capabilities:
            return (
                f"tenant {self.id!r} does not run {capability}: its tenant file lists the "
                f"capabilities it runs ({', '.join(self.capabilities) or 'none'})"
            )
        return None

    @property
    def policy_file(self) -> Path:
        return Path(self.policy) if self.policy else DEFAULT_POLICY

    @property
    def app_secrets(self) -> list[str]:
        """Secrets for the target app. Those under ``cua/`` are the system's
        own (the approval signing key) and are never offered to an agent."""
        return [key for key in self.secrets if not key.startswith(SYSTEM_SECRET_PREFIX)]

    @property
    def origin(self) -> str:
        scheme, _, rest = self.base_url.partition("://")
        return f"{scheme}://{rest.split('/', 1)[0]}"


def load_tenant(name_or_path: str, *, root: Path = TENANTS_DIR) -> Tenant:
    """Load ``tenants/<name>.yaml``, or a tenant file given by path."""
    path = Path(name_or_path)
    if path.suffix not in (".yaml", ".yml"):
        path = root / f"{name_or_path}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Tenant.model_validate(data)


def shared_bindings(tenants: list[Tenant]) -> list[str]:
    """Secrets two tenants bind to the same source, as messages.

    Isolation between tenants is only as good as the separation of their
    secrets. Two tenants reading one approval signing key means whoever may
    consent for one can sign consent for the other (a token names its tenant,
    but the claim is only as trustworthy as the key that signed it); two
    reading one credential means one tenant's runs sign on as the other's
    operator."""
    seen: dict[tuple[str, str], str] = {}
    out = []
    for tenant in tenants:
        for key, binding in tenant.secrets.items():
            source = (binding.provider, binding.var or Path(binding.path or "").as_posix())
            owner = seen.setdefault(source, f"{tenant.id}/{key}")
            if not owner.startswith(f"{tenant.id}/"):
                out.append(
                    f"secret://{tenant.id}/{key} and secret://{owner} are both bound to "
                    f"{binding.source}; each tenant needs its own"
                )
    return out
