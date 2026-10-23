"""PR17 qualification refuses incomplete, stale and substituted recovery evidence."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from scripts.ci.check_service_release import EXERCISES, MANUAL_GATES, VARIANTS, check
from scripts.ci.run_service_exercise import run


def evidence(path):
    return {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def candidate(tmp_path):
    exercises = {}
    for name, selectors in EXERCISES.items():
        suite = ET.Element("testsuite")
        for selector in selectors:
            module, function = selector.split("::")
            names = [f"{function}[{v}]" for v in VARIANTS.get(function, ())] or [function]
            for test_name in names:
                ET.SubElement(
                    suite,
                    "testcase",
                    classname=module.removesuffix(".py").replace("/", "."),
                    name=test_name,
                )
        path = tmp_path / f"{name}.xml"
        ET.ElementTree(suite).write(path)
        exercises[name] = evidence(path)
    review = tmp_path / "review.txt"
    review.write_text("Synthetic fixture only, not release evidence.", encoding="utf-8")
    record = {
        "schema_version": 1,
        "commit": "a" * 40,
        "source_dirty": False,
        "environment": "synthetic test",
        "exercises": exercises,
        "manual_gates": {
            name: {"status": "passed", "evidence": evidence(review)} for name in MANUAL_GATES
        },
        "open_blockers": [],
    }
    return tmp_path / "record.json", record


def write(candidate):
    path, record = candidate
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_complete_evidence_is_only_ready_for_review(candidate):
    assert check(write(candidate), commit="a" * 40)["status"] == "ready_for_review"


@pytest.mark.parametrize("name", EXERCISES)
@pytest.mark.parametrize(
    "mutation", ["missing", "changed", "outside", "wrong-test", "skip", "fail"]
)
def test_exercises_require_intact_executed_named_cases(candidate, name, mutation):
    path, record = candidate
    item = record["exercises"][name]
    report = path.parent / item["path"]
    if mutation == "missing":
        del record["exercises"][name]
    elif mutation == "changed":
        report.write_text("substituted")
    elif mutation == "outside":
        item["path"] = "../outside.xml"
    else:
        tree = ET.parse(report)
        case = next(tree.getroot().iter("testcase"))
        if mutation == "wrong-test":
            case.set("name", "unrelated_test")
        else:
            ET.SubElement(case, "skipped" if mutation == "skip" else "failure")
        tree.write(report)
        record["exercises"][name] = evidence(report)
    with pytest.raises((ValueError, OSError)):
        check(write(candidate), commit="a" * 40)


@pytest.mark.parametrize("name", MANUAL_GATES)
@pytest.mark.parametrize("mutation", ["pending", "failed", "no-evidence", "missing"])
def test_manual_gates_require_reviewed_evidence(candidate, name, mutation):
    gates = candidate[1]["manual_gates"]
    if mutation == "missing":
        del gates[name]
    elif mutation == "no-evidence":
        gates[name]["evidence"] = None
    else:
        gates[name]["status"] = mutation
    with pytest.raises(ValueError):
        check(write(candidate), commit="a" * 40)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_dirty", True),
        ("commit", "b" * 40),
        ("open_blockers", ["pending migration"]),
        ("schema_version", 2),
        ("source_dirty", "false"),
        ("unknown", True),
    ],
)
def test_stale_dirty_blocked_or_malformed_records_fail(candidate, field, value):
    candidate[1][field] = value
    with pytest.raises(ValueError):
        check(write(candidate), commit="a" * 40)


def test_runner_refuses_overwriting_existing_evidence(tmp_path):
    # Check the actual runner's refusal before it can launch any pytest child.
    with pytest.raises(FileExistsError):
        run(tmp_path)


@pytest.mark.parametrize("failure", [None, "exit", "timeout"])
def test_runner_records_failures_and_leaves_release_gates_pending(candidate, monkeypatch, failure):
    path, _ = candidate
    out = path.parent / "exercise"

    def git(args, **kwargs):
        return "a" * 40 if "rev-parse" in args else ""

    def pytest_child(args, **kwargs):
        report = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--junitxml="))
        if report.endswith("concurrency.xml") and failure:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 120)
            return subprocess.CompletedProcess(args, 1)
        shutil.copyfile(path.parent / report.replace("\\", "/").rsplit("/", 1)[1], report)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("scripts.ci.run_service_exercise.subprocess.check_output", git)
    monkeypatch.setattr("scripts.ci.run_service_exercise.subprocess.run", pytest_child)
    summary = run(out)
    assert summary["status"] == ("failed" if failure else "automated_checks_passed")
    assert summary["release_status"] == "not_qualified"
    assert bool(summary["failures"]) == bool(failure)
    assert (out / "summary.json").is_file()
    assert not list(out.glob("runtime-*"))
    record = json.loads((out / "record.json").read_text(encoding="utf-8"))
    assert record["source_dirty"] is False
    assert record["open_blockers"]
    assert all(gate["status"] == "pending" for gate in record["manual_gates"].values())
    with pytest.raises(ValueError):
        check(out / "record.json", commit="a" * 40)


def test_cli_failure_is_actionable_without_traceback(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.ci.check_service_release",
            str(tmp_path / "missing.json"),
            "--commit",
            "a" * 40,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "service release not ready:" in result.stderr
    assert "Traceback" not in result.stderr
