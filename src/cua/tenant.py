"""Tenant binding: the per-deployment facts a capability must not carry.

A capability is written against an application family ("legacy-core"), not
against one credit union's server. ``tenants/<id>.yaml`` says where that app
lives for one tenant and where its credentials come from, so the same
capability runs anywhere the family is deployed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

TENANTS_DIR = Path("tenants")


class SecretBinding(BaseModel):
    """Where one secret's value comes from. Never the value itself."""

    model_config = ConfigDict(frozen=True)

    provider: Literal["env"] = "env"
    var: str
    format: str = "value"
    """How the raw value splits into fields: ``username:password`` means the
    text before the first colon is ``username`` and the rest is ``password``."""

    @property
    def fields(self) -> list[str]:
        return self.format.split(":")


class Tenant(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    app_family: str
    base_url: str
    secrets: dict[str, SecretBinding] = Field(default_factory=dict)
    overlay: str | None = None

    def url(self, path: str) -> str:
        """An absolute URL on this tenant's deployment."""
        if path.startswith(("http://", "https://")):
            return path
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

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
