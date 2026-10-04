"""Regression coverage for the first adoption fixes."""

import json

import pytest
from playwright.sync_api import Error

from cua.artifact.schema import SCHEMA_VERSION, InputSpec, json_schema
from cua.cli import main
from cua.replay.invocation import Budget, check_input


@pytest.mark.parametrize(
    "value", ["NaN", "-NaN", "+NaN", "sNaN", "-sNaN", "Infinity", "-Infinity", "+Infinity"]
)
def test_decimal_inputs_must_be_finite(value):
    error = check_input("amount", value, InputSpec(type="decimal", sensitive=True))
    assert error is not None
    assert "***" in error and value not in error


@pytest.mark.parametrize("value", ["0", "-0", "12.50", "-12.5", "1e3", "1E-3"])
def test_finite_decimal_inputs(value):
    assert check_input("amount", value, InputSpec(type="decimal")) is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_budget_must_be_finite(value):
    with pytest.raises(ValueError):
        Budget(timeout_s=value)


@pytest.mark.parametrize("explicit", [False, True])
def test_schema_command_filename_and_content(tmp_path, monkeypatch, explicit):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / (
        "custom.json" if explicit else f"capabilities/schema/capability-{SCHEMA_VERSION}.json"
    )
    assert main(["schema", *(["--out", str(path)] if explicit else [])]) == 0
    assert json.loads(path.read_text()) == json_schema()


@pytest.mark.parametrize("required", [False, True])
def test_unexpected_browser_launch_errors_fail(required):
    from tests.conftest import browser_launch_error

    error = Error("BrowserType.launch: permission denied")
    with pytest.raises(Error) as caught:
        browser_launch_error(error, required=required)
    assert caught.value is error


@pytest.mark.parametrize("required", [False, True])
def test_missing_browser_only_skips_in_optional_mode(required):
    from tests.conftest import browser_launch_error

    error = Error("BrowserType.launch: Executable doesn't exist at /chromium")
    with pytest.raises(Error if required else pytest.skip.Exception):
        browser_launch_error(error, required=required)


@pytest.mark.parametrize("value", ["NaN", "-NaN", "sNaN", "-sNaN", "Infinity", "-Infinity"])
def test_nonfinite_outputs_fail(value):
    from cua.artifact.store import load
    from cua.replay.extract import ParseError, parse
    from tests.conftest import REPO_ROOT

    spec = load(REPO_ROOT / "capabilities/member_savings_balance.json").outputs["savings_balance"]
    with pytest.raises(ParseError):
        parse(value, spec)


@pytest.mark.parametrize("value", ["NaN", "-sNaN", "Infinity", "-Infinity"])
def test_invalid_decimal_starts_no_surface(tmp_path, value):
    from cua.replay.invocation import Invocation
    from cua.replay.result import Failure
    from cua.replay.runner import replay
    from tests.unit.test_replay import GOAL2, POLICY, TENANT

    def no_surface():
        raise AssertionError("surface opened for invalid inputs")

    result = replay(
        GOAL2,
        tenant=TENANT,
        policy=POLICY,
        invocation=Invocation(inputs={"member_id": "10003", "initial_deposit": value}),
        runs_dir=tmp_path / "runs",
        surface=no_surface,
    )
    assert isinstance(result, Failure)
    assert result.code == "INPUT_INVALID" and result.side_effect == "none"
    assert not (tmp_path / "runs").exists()


def test_strict_browser_mode_fails_when_no_browser_tests_execute(monkeypatch):
    from types import SimpleNamespace

    import tests.conftest as fixtures

    monkeypatch.setattr(fixtures, "_browser_executed", 0)
    session = SimpleNamespace(
        config=SimpleNamespace(
            getoption=lambda option: True,
            pluginmanager=SimpleNamespace(get_plugin=lambda name: None),
        ),
        exitstatus=0,
    )
    fixtures.pytest_sessionfinish(session, 0)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
