"""Shared by the live desktop tests: DeskCalc under Windows UI Automation.

Skipped, not failed, anywhere they cannot run: off Windows, or without the
``windows`` extra (comtypes). Each test starts its own DeskCalc and gets its
own ledger file, so a commit in one is never counted in another.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cua.tenant import Tenant, load_tenant

REPO = Path(__file__).resolve().parents[2]

if sys.platform != "win32":  # pragma: no cover - collected on Windows only
    collect_ignore_glob = ["test_*.py"]
else:
    pytest.importorskip("comtypes", reason="the windows extra is not installed")


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """DeskCalc's commit oracle for this test: one line per Record."""
    path = tmp_path / "ledger.txt"
    monkeypatch.setenv("DESKCALC_LEDGER", str(path))
    return path


@pytest.fixture
def desk(monkeypatch: pytest.MonkeyPatch) -> Tenant:
    """The desk tenant, run from the repository root so its launch command's
    relative script path resolves."""
    monkeypatch.chdir(REPO)
    return load_tenant("desk", root=REPO / "tenants")


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
