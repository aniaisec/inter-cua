"""Shared fixtures.

The mock app runs as a real uvicorn subprocess rather than through
``TestClient``, because the browser has to reach it over HTTP and because
cookie-session behaviour and 303 redirect chains are part of what is under
test.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STARTUP_TIMEOUT_S = 30.0


def free_port() -> int:
    """Ask the OS for a port, then release it. Racy in theory, fine in practice."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def mockapp_url() -> Iterator[str]:
    port = free_port()
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
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
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"

    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read().decode() if proc.stdout else ""
            raise RuntimeError(f"mockapp exited during startup:\n{output}")
        try:
            if httpx.get(f"{base_url}/login", timeout=1.0).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError(f"mockapp did not come up within {STARTUP_TIMEOUT_S}s")

    try:
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


@pytest.fixture
def client(mockapp_url: str) -> Iterator[httpx.Client]:
    """A cookie-carrying client with its own fresh mock-app session."""
    with httpx.Client(base_url=mockapp_url, follow_redirects=True, timeout=30.0) as c:
        yield c


@pytest.fixture
def signed_in(client: httpx.Client) -> httpx.Client:
    response = client.post("/login", data={"F_USRID": "operator", "F_PWD": "operator"})
    assert response.status_code == 200
    return client
