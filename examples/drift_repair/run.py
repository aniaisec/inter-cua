"""Execute this directory's machine-readable scenario through ordinary entry points."""

from __future__ import annotations

import shutil
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from cua.artifact.store import load
from examples._session import EXAMPLES, Session
from examples._support import check_subset, require


def run(s: Session, stack: ExitStack) -> dict[str, Any]:
    name = s.spec["capabilities"][0]
    original = s.work / "capabilities" / f"{name}.json"
    before = original.read_bytes()
    failed = s.cli(
        "replay", name, *s.inputs(s.spec["inputs"]), "--inject", s.spec["inject"], expected=1
    )
    check_subset(failed, {"kind": "failure", "code": "LOCATOR_UNRESOLVED", "side_effect": "none"})
    proposed = s.cli("drift", "propose", failed["evidence"]["run_dir"], "--json")
    suite = s.work / "bench/tasks/example.yaml"
    suite.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLES / "drift_repair/tasks.yaml", suite)
    evaluated = s.cli(
        "drift",
        "evaluate",
        name,
        "--version",
        str(proposed["version"]),
        "--suite",
        str(suite),
        "--json",
    )
    require(evaluated["passed"], "candidate evaluation failed")
    candidate = (
        s.work / "capabilities/candidates" / name / f"v{proposed['version']}" / "capability.json"
    )
    require(load(candidate).approval_state == "draft", "evaluation approved its candidate")
    refused = s.cli("replay", str(candidate), *s.inputs(s.spec["inputs"]), expected=1)
    require(refused["code"] == "POLICY_BLOCKED", "unreviewed repair replayed")
    require(original.read_bytes() == before, "candidate changed the original")
    s.no_models(failed)
    for task in evaluated["tasks"]:
        require(task["candidate"]["exact"] == 1, "candidate gave a wrong answer")
    evaluation_runs = list(Path(evaluated["runs_dir"]).rglob("result.json"))
    require(len(evaluation_runs) == len(evaluated["tasks"]) * 2, "evaluation evidence missing")
    for path in evaluation_runs:
        s.no_models({"evidence": {"run_dir": str(path.parent)}})
    return {
        "original_refused": True,
        "evaluation_passed": True,
        "candidate_state": "draft",
        "original_unchanged": True,
        "review_required": True,
    }
