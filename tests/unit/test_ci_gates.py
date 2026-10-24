"""An empty, skipped, failed or malformed report cannot certify a release."""

from types import SimpleNamespace

import pytest

from scripts.ci.check_test_results import check


def test_every_surface_feature_has_an_executable_contract_probe():
    from cua.surface.features import FEATURES
    from tests.contracts.surface import FEATURE_CONTRACTS, SurfaceContract

    assert set(FEATURE_CONTRACTS) == set(FEATURES)
    assert all(callable(getattr(SurfaceContract, name)) for name in FEATURE_CONTRACTS.values())


def test_compatibility_workflow_assigns_every_acceptance_gate():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    workflow = yaml.load((root / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]
    assert set(jobs) == {
        "quality",
        "browser",
        "examples",
        "security",
        "windows",
        "providers",
        "installed",
        "service-recovery",
    }
    for name in ("quality", "browser"):
        assert jobs[name]["strategy"]["matrix"]["python"] == ["3.11", "3.14"]
        commands = "\n".join(step.get("run", "") for step in jobs[name]["steps"])
        assert "--ignore=tests/unit/test_llm_providers.py" in commands
    for name in ("quality", "browser", "windows", "examples", "security"):
        commands = "\n".join(step.get("run", "") for step in jobs[name]["steps"])
        assert "check_no_providers.py" in commands and ".[dev]" not in commands
    assert workflow["permissions"] == {"contents": "read"}
    assert all(
        "secrets." not in str(job) and "self-hosted" not in str(job) for job in jobs.values()
    )
    assert "coverage.json" in str(jobs["quality"]) and "check_coverage.py" in str(jobs["quality"])
    desktop_type_checks = [
        step["run"]
        for step in jobs["quality"]["steps"]
        if "scripts/ci/run_desktop_gate.py" in step.get("run", "")
    ]
    assert desktop_type_checks
    assert all("--platform win32" in command for command in desktop_type_checks)
    assert "tests/integration/test_examples.py --require-browser" in str(jobs["examples"])
    assert "tests/security --require-browser" in str(jobs["security"])
    recovery = jobs["service-recovery"]
    assert recovery["strategy"]["matrix"]["os"] == ["ubuntu-latest", "windows-latest"]
    assert recovery["runs-on"] == "${{ matrix.os }}"
    assert any(step.get("with", {}).get("python-version") == "3.14" for step in recovery["steps"])
    assert any(
        step.get("run")
        == "python -m scripts.ci.run_service_exercise --out artifacts/service-recovery"
        for step in recovery["steps"]
    )


def complete_coverage():
    from scripts.ci.check_coverage import TARGETS

    return {
        "meta": {"branch_coverage": True},
        "files": {
            path: {
                "functions": {
                    function: {
                        "summary": {
                            "num_statements": 2,
                            "num_branches": 2,
                            "missing_lines": 0,
                            "missing_branches": 0,
                        }
                    }
                    for function in functions
                }
            }
            for path, functions in TARGETS.items()
        },
    }


@pytest.mark.parametrize(
    "failure",
    [
        "no-branches",
        "missing-file",
        "missing-function",
        "missing_lines",
        "missing_branches",
        "num_statements",
        "num_branches",
    ],
)
def test_critical_coverage_cannot_pass_without_complete_branch_evidence(failure):
    from scripts.ci.check_coverage import check as check_coverage

    report = complete_coverage()
    if failure == "no-branches":
        report["meta"]["branch_coverage"] = False
    elif failure == "missing-file":
        report["files"].pop("src/cua/catalog.py")
    elif failure == "missing-function":
        report["files"]["src/cua/catalog.py"]["functions"].clear()
    else:
        report["files"]["src/cua/catalog.py"]["functions"]["_as_text"]["summary"][failure] = (
            0 if failure.startswith("num_") else 1
        )
    with pytest.raises(ValueError):
        check_coverage(report)


def test_critical_coverage_accepts_complete_evidence_on_both_path_styles():
    from scripts.ci.check_coverage import check as check_coverage

    report = complete_coverage()
    assert len(check_coverage(report)) == 5
    report["files"] = {name.replace("/", "\\"): data for name, data in report["files"].items()}
    assert len(check_coverage(report)) == 5


@pytest.mark.parametrize(
    "body",
    [
        "<testsuites/>",
        '<testsuite><testcase><skipped message="missing UIA"/></testcase></testsuite>',
        "<testsuite><testcase><failure/></testcase></testsuite>",
        '<testsuite errors="1"><testcase/></testsuite>',
        '<testsuite><testcase/><testcase><skipped message="Chromium unavailable"/>'
        "</testcase></testsuite>",
        "<other><testcase/></other>",
    ],
)
def test_incomplete_test_reports_fail_the_gate(tmp_path, body):
    path = tmp_path / "results.xml"
    path.write_text(body)
    with pytest.raises(ValueError):
        check(path)


def test_optional_feature_skips_are_named_and_do_not_count_as_executed(tmp_path):
    path = tmp_path / "results.xml"
    path.write_text(
        '<testsuite><testcase name="refs"/><testcase name="pointer">'
        '<skipped message="adapter does not claim pointer"/></testcase></testsuite>'
    )
    report = check(path, allow_skip_prefix=("adapter does not claim ",))
    assert report == {
        "tests": 2,
        "executed": 1,
        "passed": 1,
        "skipped": 1,
        "skipped_cases": ["pointer"],
    }


def test_strict_desktop_gate_refuses_zero_executed_cases(monkeypatch):
    import tests.conftest as fixtures

    monkeypatch.setattr(fixtures, "_desktop_executed", 0)
    session = SimpleNamespace(
        config=SimpleNamespace(
            getoption=lambda name: name == "--require-desktop",
            pluginmanager=SimpleNamespace(get_plugin=lambda name: None),
        ),
        exitstatus=0,
    )
    fixtures.pytest_sessionfinish(session, 0)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED


def test_strict_desktop_gate_refuses_non_windows(monkeypatch):
    import tests.conftest as fixtures

    monkeypatch.setattr(fixtures.sys, "platform", "linux")
    with pytest.raises(pytest.UsageError, match="interactive Windows"):
        fixtures.pytest_configure(SimpleNamespace(getoption=lambda name: True))


def test_a_new_test_session_cannot_reuse_previous_browser_or_desktop_execution(monkeypatch):
    import tests.conftest as fixtures

    monkeypatch.setattr(fixtures, "_browser_executed", 3)
    monkeypatch.setattr(fixtures, "_desktop_executed", 2)
    fixtures.pytest_sessionstart(None)
    assert fixtures._browser_executed == fixtures._desktop_executed == 0
