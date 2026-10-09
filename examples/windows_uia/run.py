"""Execute this directory's machine-readable scenario through ordinary entry points."""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any

from examples._session import Session
from examples._support import check_subset, require


def run(s: Session, stack: ExitStack) -> dict[str, Any]:
    from cua.policy.allowlist import load_policy
    from cua.project import ProjectContext
    from cua.replay.engine import ReplayConfig
    from cua.replay.invocation import Invocation
    from cua.replay.runner import replay
    from cua.tenant import load_tenant

    done = s.replay("deskcalc_compute", s.spec["inputs"])
    check_subset(done, {"kind": "success", "outputs": s.spec["outputs"]})
    s.no_models(done)
    # The same real capability with an explicitly unsupported evidence feature
    # must be refused before launching the desktop target.
    project = ProjectContext.resolve(s.work, environ=s.env)
    tenant = load_tenant("local", project=project)
    refused = replay(
        s.work / "capabilities/deskcalc_compute.json",
        tenant=tenant,
        policy=load_policy(tenant.policy_file, tenant),
        invocation=Invocation(inputs=s.spec["inputs"]),
        runs_dir=s.out / "unsupported-runs",
        config=ReplayConfig(screenshots=True),
        environ=project.environ,
    ).model_dump(mode="json")
    s.record(["replay", "ReplayConfig(screenshots=True)"], 1, refused)
    check_subset(
        refused, {"kind": "failure", "code": "SURFACE_INCOMPATIBLE", "side_effect": "none"}
    )
    require(refused["evidence"]["run_dir"] is None, "unsupported feature started an application")
    return {"kind": "success", "unsupported_refused": True, "replay_model_calls": 0}
