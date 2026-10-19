"""Incomplete, stale or substituted evidence cannot qualify an adoption candidate."""

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.ci.check_adoption_release import GATES, check


@pytest.fixture
def candidate(tmp_path):
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("inter_cua-0.1.0.dist-info/METADATA", "Name: inter-cua\nVersion: 0.1.0\n")

    def evidence(path):
        return {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    report = tmp_path / "review.md"
    report.write_text("Synthetic gate report fixture, not release evidence.")
    data = {
        "schema_version": 1,
        "version": "0.1.0",
        "commit": "a" * 40,
        "wheel": evidence(wheel),
        "gates": {name: {"status": "passed", "evidence": evidence(report)} for name in GATES},
        "trials": [
            {
                "participant": name,
                "platform": "windows",
                "unfamiliar_with_code": True,
                "completed": True,
                "no_source_edits": True,
                "demo_minutes": 12.0,
                "capability_minutes": 40.0,
                "evidence": evidence(report),
            }
            for name in ("A", "B")
        ],
        "open_blockers": [],
    }
    return tmp_path / "record.json", data


def write_record(candidate):
    path, data = candidate
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_complete_record_reports_goals_without_rejecting_slow_trials(candidate):
    _, data = candidate
    data["trials"][1]["demo_minutes"] = 16.0
    data["trials"][1]["capability_minutes"] = 70.0
    result = check(write_record(candidate))
    assert result["status"] == "ready_for_review"
    assert result["timing_goals_missed"] == ["B"]


@pytest.mark.parametrize("gate", GATES)
@pytest.mark.parametrize("mutation", ["missing", "pending", "failed", "no-evidence"])
def test_every_gate_requires_passing_evidence(candidate, gate, mutation):
    gates = candidate[1]["gates"]
    if mutation == "missing":
        del gates[gate]
    elif mutation == "no-evidence":
        gates[gate]["evidence"] = None
    else:
        gates[gate]["status"] = mutation
    with pytest.raises(ValueError):
        check(write_record(candidate))


@pytest.mark.parametrize(
    "mutation",
    [
        "one-trial",
        "duplicate",
        "familiar",
        "incomplete",
        "source-edit",
        "blocker",
        "version",
        "negative",
        "infinite",
        "unknown-field",
        "unknown-gate",
        "string-bool",
        "bad-commit",
    ],
)
def test_ineligible_or_malformed_records_fail(candidate, mutation):
    data = candidate[1]
    trial = data["trials"][1]
    if mutation == "one-trial":
        data["trials"].pop()
    elif mutation == "duplicate":
        trial["participant"] = " a "
    elif mutation in ("familiar", "incomplete", "source-edit"):
        field = {
            "familiar": "unfamiliar_with_code",
            "incomplete": "completed",
            "source-edit": "no_source_edits",
        }[mutation]
        trial[field] = False
    elif mutation == "blocker":
        data["open_blockers"] = ["Tutorial needs a source edit"]
    elif mutation == "version":
        data["version"] = "9.9.9"
    elif mutation in ("negative", "infinite"):
        trial["demo_minutes"] = -1.0 if mutation == "negative" else float("inf")
    elif mutation == "unknown-field":
        data["approve_anyway"] = True
    elif mutation == "unknown-gate":
        data["gates"]["shortcut"] = {"status": "passed"}
    elif mutation == "string-bool":
        trial["completed"] = "true"
    else:
        data["commit"] = "main"
    with pytest.raises(ValueError):
        check(write_record(candidate))


@pytest.mark.parametrize("target", ["wheel", "gate", "trial"])
@pytest.mark.parametrize("mutation", ["missing", "changed", "outside"])
def test_evidence_must_exist_match_and_stay_in_bundle(candidate, target, mutation):
    path, data = candidate
    item = {
        "wheel": data["wheel"],
        "gate": data["gates"]["quality"]["evidence"],
        "trial": data["trials"][0]["evidence"],
    }[target]
    if mutation == "missing":
        item["path"] = "missing.txt"
    elif mutation == "changed":
        (path.parent / item["path"]).write_bytes(b"substituted report")
    else:
        item["path"] = "../outside.txt"
    with pytest.raises((OSError, ValueError)):
        check(write_record(candidate))


def test_cli_rejects_pending_template_without_traceback():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.ci.check_adoption_release",
            "docs/release/adoption-record.template.json",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "adoption release not ready:" in result.stderr
    assert "Traceback" not in result.stderr
