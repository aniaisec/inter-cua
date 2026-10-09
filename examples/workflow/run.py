"""Execute this directory's machine-readable scenario through ordinary entry points."""

from __future__ import annotations

import shutil
from contextlib import ExitStack
from typing import Any

from examples._session import EXAMPLES, Session
from examples._support import check_subset, require


def run(s: Session, stack: ExitStack) -> dict[str, Any]:
    path = s.work / "workflows/lookup_twice.yaml"
    path.parent.mkdir(exist_ok=True)
    shutil.copyfile(EXAMPLES / "workflow/workflow.yaml", path)
    s.cli("workflow", "check", "lookup_twice", "--json")
    before = s.oracle()
    done = s.cli("workflow", "run", "lookup_twice", *s.inputs(s.spec["inputs"]))
    check_subset(done, {"kind": "success", "outputs": s.spec["outputs"]})
    failed = s.cli(
        "workflow", "run", "lookup_twice", *s.inputs(s.spec["rejected_inputs"]), expected=2
    )
    require(
        [step["kind"] for step in failed["steps"]] == ["business_outcome", "not_run"],
        "workflow ran a later step after business rejection",
    )
    for result in (done, failed):
        for step in result["steps"]:
            if step["run_dir"]:
                s.no_models({"evidence": {"run_dir": step["run_dir"]}})
    require(s.oracle() == before, "read-only workflow changed state")
    return {
        "binding_validated": True,
        "rejected_step": failed["step_id"],
        "later_step": failed["steps"][1]["kind"],
        "replay_model_calls": 0,
    }
