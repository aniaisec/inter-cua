"""Resolve ``secret://<tenant>/<key>`` references into values held in memory.

A capability names its credentials by reference so that it can be committed,
reviewed and shared across tenants without carrying anything secret. The
reference is resolved at run time through the tenant binding, and the value
lives only in a ``Credential`` object: its ``repr`` is masked, it is never
serialized, and the redaction layer is handed every value so it can scrub the
places a value leaks into by itself (a password field's value shows up in the
accessibility tree, for instance).
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping

from cua.tenant import Tenant

SCHEME = "secret://"


class SecretError(Exception):
    """A reference that cannot be resolved. Never carries the value."""


class Credential:
    """The fields of one resolved secret. Deliberately not a pydantic model:
    nothing should be able to dump it."""

    __slots__ = ("_fields", "ref")

    def __init__(self, ref: str, fields: Mapping[str, str]) -> None:
        self.ref = ref
        self._fields = dict(fields)

    def field(self, name: str) -> str:
        if name not in self._fields:
            raise SecretError(f"{self.ref} has no field {name!r}; it has {sorted(self._fields)}")
        return self._fields[name]

    @property
    def field_names(self) -> list[str]:
        return list(self._fields)

    def values(self) -> Iterator[str]:
        return iter(v for v in self._fields.values() if v)

    def __repr__(self) -> str:
        return f"Credential({self.ref!r}, fields={self.field_names}, values=***)"


def parse_ref(ref: str) -> tuple[str, str]:
    """``secret://local/mockcore/operator`` → ``("local", "mockcore/operator")``."""
    if not ref.startswith(SCHEME):
        raise SecretError(f"not a secret reference: {ref!r}")
    tenant, _, key = ref[len(SCHEME) :].partition("/")
    if not tenant or not key:
        raise SecretError(f"a secret reference is secret://<tenant>/<key>, got {ref!r}")
    return tenant, key


def resolve(ref: str, tenant: Tenant, *, environ: Mapping[str, str] | None = None) -> Credential:
    """Look the reference up in the tenant binding and read its value."""
    tenant_id, key = parse_ref(ref)
    if tenant_id != tenant.id:
        raise SecretError(f"{ref} belongs to tenant {tenant_id!r}, not {tenant.id!r}")
    binding = tenant.secrets.get(key)
    if binding is None:
        raise SecretError(f"tenant {tenant.id!r} binds no secret {key!r}")

    env = os.environ if environ is None else environ
    raw = env.get(binding.var)
    if not raw:
        raise SecretError(f"{ref} is bound to ${binding.var}, which is not set")

    names = binding.fields
    parts = raw.split(":", len(names) - 1)
    if len(parts) != len(names):
        raise SecretError(f"${binding.var} does not have the shape {binding.format!r}")
    return Credential(ref, dict(zip(names, parts, strict=True)))
