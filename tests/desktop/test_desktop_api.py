"""A desktop capability invoked over HTTP. The API executes runs on worker
threads, and UI Automation is COM, which each thread initialises for itself:
this is the test that the worker's initialisation is enough."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cua.api.access import AccessConfig, Gate
from cua.api.app import create_app
from cua.api.service import RunService, ServiceSettings
from cua.tenant import Tenant
from tests.desktop.conftest import REPO, lines

pytestmark = pytest.mark.desktop

KEY = "desktop-api-key-" + "d" * 32


def test_deskcalc_compute_over_http(desk: Tenant, ledger: Path, tmp_path: Path) -> None:
    config = AccessConfig.model_validate(
        {
            "tenants": {"desk": {"policy": str(REPO / "policies" / "deskcalc.yaml")}},
            "clients": {
                "agent": {
                    "key": {"provider": "env", "var": "KEY"},
                    "tenants": ["desk"],
                    "capabilities": ["deskcalc_compute"],
                    "scopes": ["read", "invoke"],
                }
            },
        }
    )
    gate = Gate(config, environ={"KEY": KEY}, tenants_dir=REPO / "tenants")
    service = RunService(
        ServiceSettings(runs_dir=tmp_path / "runs", capabilities_dir=REPO / "capabilities")
    )
    with TestClient(create_app(gate, service)) as api:
        r = api.post(
            "/runs?wait=120",
            json={
                "capability": "deskcalc_compute",
                "inputs": {"first": "12.5", "second": "4", "operation": "Multiply"},
            },
            headers={
                "Authorization": f"Bearer {KEY}",
                "X-Cua-Tenant": "desk",
                "X-Request-Id": "desk-1",
            },
        )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["kind"] == "success", result
    assert result["outputs"]["result"] == "50.0"
    assert lines(ledger) == []  # computing records nothing
