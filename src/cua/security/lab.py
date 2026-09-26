"""The world a security session runs in.

A copy of the capabilities (tampering scenarios edit it, never the repo's),
a tenant whose operator password is a **canary** — a random value the mock
app is started with and that appears nowhere else — and a signing key of the
session's own. Live scenarios get a fresh mock app on a free port; its
debug endpoints are the oracles: ``/_debug/stats`` counts commits,
``/_debug/attacker`` counts what reached the attacker's origin and the files
the app served.
"""

from __future__ import annotations

import secrets
import shutil
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from cua.policy.allowlist import Policy, load_policy
from cua.policy.tokens import SIGNING_KEY
from cua.tenant import SecretBinding, Tenant, load_tenant

OPERATOR_VAR = "CUA_SEC_OPERATOR"
SIGNING_VAR = "CUA_SEC_SIGNING_KEY"
OFFLINE_URL = "http://127.0.0.1:9"
"""Where an offline lab's tenant points: nothing listens there, and nothing
offline goes there — a probe that started a browser would fail loudly."""


@dataclass(frozen=True)
class Lab:
    root: Path
    tenant: Tenant
    policy: Policy
    environ: dict[str, str]
    canary: str
    """The operator password for this session."""
    live: bool

    @property
    def capabilities(self) -> Path:
        return self.root / "capabilities"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def secrets(self) -> list[str]:
        return [self.canary, self.environ[SIGNING_VAR]]

    # -- oracles (live only) ------------------------------------------------

    def counters(self) -> dict[str, int]:
        stats = httpx.get(self.tenant.url("/_debug/stats"), timeout=5.0).json()
        attacker = self.attacker()
        return {
            "commits": int(stats["confirms_total"]),
            "attacker": int(attacker["received"]),
            "files": int(attacker["files_served"]),
        }

    def attacker(self) -> dict[str, Any]:
        data: dict[str, Any] = httpx.get(self.tenant.url("/_debug/attacker"), timeout=5.0).json()
        return data


def new_canary() -> str:
    return "cnry" + secrets.token_hex(8)


def make_lab(root: Path, base_url: str | None, *, canary: str | None = None) -> Lab:
    root.mkdir(parents=True, exist_ok=True)
    caps = root / "capabilities"
    if caps.exists():
        shutil.rmtree(caps)
    shutil.copytree(Path("capabilities"), caps, ignore=shutil.ignore_patterns("candidates"))
    canary = canary or new_canary()
    base = load_tenant("local")
    tenant = base.model_copy(
        update={
            "base_url": base_url or OFFLINE_URL,
            "secrets": {
                "mockcore/operator": SecretBinding(var=OPERATOR_VAR, format="username:password"),
                SIGNING_KEY: SecretBinding(var=SIGNING_VAR),
            },
        }
    )
    return Lab(
        root=root,
        tenant=tenant,
        policy=load_policy(Path("policies/default.yaml"), tenant),
        environ={OPERATOR_VAR: f"operator:{canary}", SIGNING_VAR: secrets.token_hex(32)},
        canary=canary,
        live=base_url is not None,
    )


def exposures(
    secrets_: Iterable[str], *, dirs: Iterable[Path] = (), texts: Iterable[str] = ()
) -> list[str]:
    """Where any of the secrets appears: files (a zip's members too) and texts."""
    needles = [s.encode("utf-8") for s in secrets_ if s]
    found: list[str] = []
    for d in dirs:
        if not d.exists():
            continue
        for path in sorted(p for p in d.rglob("*") if p.is_file()):
            for name, data in _contents(path):
                if any(n in data for n in needles):
                    found.append(name)
    for i, text in enumerate(texts):
        if any(n.decode("utf-8") in text for n in needles):
            found.append(f"text #{i}")
    return found


def _contents(path: Path) -> list[tuple[str, bytes]]:
    data = path.read_bytes()
    if path.suffix == ".zip":
        try:
            with zipfile.ZipFile(path) as z:
                return [(f"{path.as_posix()}!{n}", z.read(n)) for n in z.namelist()]
        except zipfile.BadZipFile:
            pass
    return [(path.as_posix(), data)]
