"""Shared inputs for the artifact tests: the committed goal-1 run and what the
recorder makes of it.

``tests/fixtures/runs/goal1-scripted`` is a real discovery run against the mock
app, made with the bundled script as the model (``--llm scripted``, no
screenshots), so it can be committed: ``evidence/runs`` is not. The golden
capability pins its id, the one thing that differs on every call.

Regenerate the golden after an intended recorder change with::

    CUA_UPDATE_GOLDEN=1 pytest tests/unit/test_recorder.py
"""

from __future__ import annotations

from pathlib import Path

from cua.artifact.recorder import record
from cua.artifact.schema import Capability
from cua.policy.allowlist import Policy, load_policy
from cua.tenant import Tenant

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
GOAL1_RUN = FIXTURES / "runs" / "goal1-scripted"
GOLDEN = FIXTURES / "golden" / "member_savings_balance.json"
FAMILIES = REPO / "capabilities" / "families"

FIXED_ID = "cap_01M2TD00000000000000000000"
TENANT = Tenant(id="local", app_family="legacy-core", base_url="http://127.0.0.1:8000")


def policy() -> Policy:
    return load_policy(REPO / "policies" / "default.yaml", TENANT)


def record_goal1(run_dir: Path = GOAL1_RUN) -> Capability:
    return record(
        run_dir,
        policy=policy(),
        families_dir=FAMILIES,
        capability_id=FIXED_ID,
    )
