"""CI must reject incomplete evidence and replay regressions."""

import json

import pytest

from scripts.ci.check_benchmark import TASKS, check


@pytest.fixture
def reports(tmp_path):
    session = {
        "session_id": "smoke",
        "started_at": "2026-10-02",
        "finished_at": "2026-10-02",
        "suite": "core",
        "tasks": sorted(TASKS),
        "strategies": ["inter_cua_replay"],
        "llm": "scripted",
        "python": "3.11",
        "platform": "linux",
        "runs": 6,
    }
    rows = [
        {
            "session_id": "smoke",
            "task_id": task,
            "strategy": "inter_cua_replay",
            "repetition": rep,
            "run_id": f"{task}-{rep}",
            "started_at": "2026-10-02",
            "match": "exact",
            "success": True,
            "outcome": "success",
            "truth": "answer",
            "duration_s": 1,
            "commits_observed": 0,
        }
        for task in sorted(TASKS)
        for rep in (1, 2)
    ]
    (tmp_path / "sessions.jsonl").write_text(json.dumps(session) + "\n")
    (tmp_path / "runs.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")
    return tmp_path


def test_complete_smoke_passes(reports):
    check(reports)


@pytest.mark.parametrize(
    "field,value",
    [
        ("llm_calls", 1),
        ("match", "wrong"),
        ("commits_observed", 1),
        ("duplicate_side_effects", 1),
        ("cached", True),
        ("run_id", None),
        ("session_id", "other"),
    ],
)
def test_replay_regressions_fail(reports, field, value):
    path = reports / "runs.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0][field] = value
    path.write_text("\n".join(map(json.dumps, rows)))
    with pytest.raises(ValueError):
        check(reports)


def test_duplicate_coverage_fails(reports):
    path = reports / "runs.jsonl"
    rows = path.read_text().splitlines()
    rows[0] = rows[1]
    path.write_text("\n".join(rows))
    with pytest.raises(ValueError):
        check(reports)


def test_missing_evidence_fails(tmp_path):
    with pytest.raises(ValueError):
        check(tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [("finished_at", None), ("llm", "gemini"), ("runs", 5), ("tasks", [])],
)
def test_incomplete_or_wrong_session_fails(reports, field, value):
    path = reports / "sessions.jsonl"
    session = json.loads(path.read_text())
    session[field] = value
    path.write_text(json.dumps(session))
    with pytest.raises(ValueError):
        check(reports)
