"""Explicit live interactive Windows gate; never inferred from a type check."""

from pathlib import Path

import pytest

from examples.run import run_scenario

pytestmark = pytest.mark.desktop


def test_documented_windows_example(tmp_path: Path) -> None:
    assert run_scenario("windows_uia", tmp_path / "windows")["passed"]
