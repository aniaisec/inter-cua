"""The world a benchmark session runs in: one fresh mock app, one tenant.

Every strategy gets the same one: the same app process, tenant binding,
policy file and credentials. The app is started fresh for the session on a
free port, so its commit count starts at zero and nothing a previous session
did (or a developer's own ``cua mockapp`` on :8000) leaks into the numbers.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

from cua.policy.allowlist import Policy, load_policy
from cua.policy.tokens import SIGNING_KEY
from cua.tenant import SecretBinding, Tenant, load_tenant

STARTUP_S = 30.0
MOCK_CREDENTIAL = "operator:operator"
"""The mock app's published sign-on (``.env.example``); used only if unset."""
OPERATOR_VAR = "CUA_SECRET_MOCKCORE_OPERATOR"
SIGNING_VAR = "CUA_BENCH_SIGNING_KEY"


class BenchSetupError(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def mockapp(port: int | None = None) -> Iterator[str]:
    """A mock app of its own for this session; yields its base URL."""
    port = port or free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "mockapp.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env={**os.environ, "PYTHONPATH": str(Path.cwd())},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + STARTUP_S
        while True:
            if proc.poll() is not None:
                raise BenchSetupError("the mock app exited during startup")
            try:
                if httpx.get(f"{base_url}/login", timeout=1.0).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise BenchSetupError(f"the mock app did not come up within {STARTUP_S:.0f} s")
            time.sleep(0.1)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


@dataclass(frozen=True)
class BenchEnv:
    tenant: Tenant
    policy: Policy
    environ: dict[str, str]
    """What secrets resolve from: the process environment, plus a signing key
    made for this session (consent minted here is good only here)."""
    runs_dir: Path

    @property
    def signing_key(self) -> bytes:
        return self.environ[SIGNING_VAR].encode("utf-8")

    def commits_total(self) -> int | None:
        """Irreversible commits the app has recorded, across all sessions.
        None if it cannot say (an app with no debug surface)."""
        try:
            response = httpx.get(self.tenant.url("/_debug/stats"), timeout=5.0)
            response.raise_for_status()
            return int(response.json()["confirms_total"])
        except (httpx.HTTPError, KeyError, ValueError):
            return None


def bench_env(
    base_url: str,
    runs_dir: Path,
    *,
    tenant: str = "local",
    policy: Path = Path("policies/default.yaml"),
) -> BenchEnv:
    """The tenant, pointed at this session's app, with its own signing key."""
    base = load_tenant(tenant)
    bound = base.model_copy(
        update={
            "base_url": base_url,
            "secrets": {
                **base.secrets,
                SIGNING_KEY: SecretBinding(provider="env", var=SIGNING_VAR),
            },
        }
    )
    environ = {
        **os.environ,
        SIGNING_VAR: secrets.token_hex(32),
    }
    environ.setdefault(OPERATOR_VAR, MOCK_CREDENTIAL)
    return BenchEnv(
        tenant=bound,
        policy=load_policy(policy, bound),
        environ=environ,
        runs_dir=runs_dir,
    )
