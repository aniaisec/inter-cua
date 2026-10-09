"""Run the same commands/scenarios that the example documentation advertises."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from examples.run import NAMES, run_scenario


@pytest.mark.browser
@pytest.mark.parametrize("name", [name for name in NAMES if name != "windows_uia"])
def test_documented_example(name: str, tmp_path: Path, browser_session, monkeypatch) -> None:
    # The browser fixture implements strict --require-browser launch checks.
    # An example must use its synthetic login even when a caller has a different
    # binding configured in their shell; never submit that binding to a demo app.
    monkeypatch.setenv("CUA_INVENTORY_LOGIN", "different-user:different-password")
    monkeypatch.setenv("CUA_SECRET_MOCKCORE_OPERATOR", "different-user:different-password")
    monkeypatch.setenv("MOCKAPP_OPERATOR_PASSWORD", "different-password")
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(run_scenario, name, tmp_path / name).result()
    assert result["passed"]
    assert (tmp_path / name / "commands.json").is_file()
    assert (tmp_path / name / "demonstration.cast").is_file()


@pytest.mark.browser
def test_examples_repeat_without_manual_reset(tmp_path: Path, browser_session) -> None:
    for attempt in range(2):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(
                run_scenario, "approved_submission", tmp_path / f"write-{attempt}"
            ).result()["passed"]
