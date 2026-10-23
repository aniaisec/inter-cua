"""Explicit operator identities, separate from API callers and capability grants.

HTTP Basic provides the browser login prompt without application session cookies.
Serve behind TLS outside loopback. Forms still require identity-bound CSRF tokens.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cua.api.access import _read_key
from cua.tenant import SecretBinding

Role = Literal["read", "operate", "approve"]


class Operator(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: SecretBinding
    tenants: dict[str, list[Role]] = Field(min_length=1)


class OperatorConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    operators: dict[str, Operator] = Field(min_length=1)

    @model_validator(mode="after")
    def identities(self) -> OperatorConfig:
        if any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) for name in self.operators
        ):
            raise ValueError("operator identity must be 1-64 ASCII letters, digits, dots, _ or -")
        for operator in self.operators.values():
            if any(not tenant or "read" not in roles for tenant, roles in operator.tenants.items()):
                raise ValueError("each tenant grant must include read")
        return self


@dataclass(frozen=True)
class Principal:
    name: str
    tenant: str
    roles: frozenset[Role]

    def permits(self, role: Role) -> bool:
        return role in self.roles


class OperatorGate:
    def __init__(
        self, config: OperatorConfig, *, root: Path, environ: dict[str, str] | None = None
    ) -> None:
        self.config = config
        self._keys: dict[str, bytes] = {}
        for name, operator in config.operators.items():
            key, _ = _read_key(operator.key, root, os.environ if environ is None else environ)
            if key is None:
                raise ValueError(
                    f"operator {name!r} needs an available key of at least 32 characters"
                )
            if key in self._keys.values():
                raise ValueError("operator credentials must be distinct")
            self._keys[name] = key
        self._csrf_key = secrets.token_bytes(32)

    @classmethod
    def load(cls, path: Path, *, root: Path, environ: dict[str, str]) -> OperatorGate:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (UnicodeError, yaml.YAMLError):
            raise ValueError("invalid operator access YAML") from None
        config = OperatorConfig.model_validate(data)
        return cls(config, root=root, environ=environ)

    def authenticate(self, authorization: str, tenant: str) -> Principal | None:
        try:
            scheme, encoded = authorization.split(" ", 1)
            if scheme.lower() != "basic":
                return None
            name, password = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
        except (ValueError, UnicodeError, binascii.Error):
            return None
        expected = self._keys.get(name, b"unconfigured-operator")
        matches = hmac.compare_digest(
            hashlib.sha256(password.encode()).digest(), hashlib.sha256(expected).digest()
        )
        if not matches or name not in self._keys:
            return None
        roles = self.config.operators[name].tenants.get(tenant)
        return Principal(name, tenant, frozenset(roles)) if roles else None

    def csrf(self, principal: Principal, request_id: str, action: str) -> str:
        stamp = str(int(time.time()))
        return stamp + "." + self._signature(principal, request_id, action, stamp)

    def _signature(self, principal: Principal, request_id: str, action: str, stamp: str) -> str:
        value = "\n".join((principal.name, principal.tenant, request_id, action, stamp)).encode()
        return hmac.new(self._csrf_key, value, hashlib.sha256).hexdigest()

    def check_csrf(self, token: str, principal: Principal, request_id: str, action: str) -> bool:
        try:
            stamp, signature = token.split(".", 1)
            age = time.time() - int(stamp)
        except ValueError:
            return False
        return 0 <= age <= 900 and hmac.compare_digest(
            signature.encode(), self._signature(principal, request_id, action, stamp).encode()
        )
