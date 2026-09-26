"""The shipped workflow against the real mock app, through the real replay.

A member lookup then a sub-account opening, with consent for the opening
only: one commit, typed outputs from both steps. The same request again is
answered without a browser and commits nothing more. A member that does not
exist stops the workflow at the lookup, before the commit.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

import httpx
import pytest

from cua.policy import tokens
from cua.policy.allowlist import load_policy
from cua.registry.store import Registry
from cua.replay.invocation import ApprovalGrant
from cua.tenant import SecretBinding, Tenant
from cua.workflow.models import WorkflowResult
from cua.workflow.runner import WorkflowRequest, run
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.browser

WORKFLOW = REPO_ROOT / "workflows" / "open_member_subaccount.yaml"
ENV = {"CUA_TEST_OPERATOR": "operator:operator", "CUA_TEST_SIGNING_KEY": "s" * 64}
T = TypeVar("T")


def apart(fn: Callable[[], T]) -> T:
    """On a thread of its own: replay starts its own sync Playwright, and the
    browser tests hold one on this thread."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn).result()


def test_the_workflow_commits_once_and_stops_before_the_commit_on_an_unknown_member(
    mockapp_url: str, tmp_path: Path
) -> None:
    caps = tmp_path / "capabilities"
    shutil.copytree(REPO_ROOT / "capabilities", caps)
    tenant = Tenant(
        id="local",
        app_family="legacy-core",
        base_url=mockapp_url,
        secrets={
            "mockcore/operator": SecretBinding(var="CUA_TEST_OPERATOR", format="username:password"),
            "cua/approval-signing-key": SecretBinding(var="CUA_TEST_SIGNING_KEY"),
        },
    )
    policy = load_policy(REPO_ROOT / "policies" / "default.yaml", tenant)
    opener = Registry(caps).version("open_subaccount", 3).capability

    def consent(inputs: dict[str, str]) -> dict[str, ApprovalGrant]:
        token = tokens.mint(opener, tenant, inputs, approved_by="anil", key=b"s" * 64)
        return {"open": ApprovalGrant(token=token)}

    def go(inputs: dict[str, str], key: str) -> WorkflowResult:
        request = WorkflowRequest(inputs=inputs, idempotency_key=key, approvals=consent(inputs))
        return apart(
            lambda: run(
                WORKFLOW,
                tenant=tenant,
                policy=policy,
                request=request,
                capabilities_dir=caps,
                runs_dir=tmp_path / "runs",
                environ=ENV,
            )
        )

    def commits() -> int:
        return int(httpx.get(f"{mockapp_url}/_debug/stats").json()["confirms_total"])

    inputs = {"member_id": "10005", "initial_deposit": "125.00"}
    before = commits()
    done = go(inputs, "wf-it-1")
    assert (done.kind, done.side_effect) == ("success", "committed"), done.message
    assert done.outputs["savings_balance_before"] and done.outputs["member_name"]
    assert str(done.outputs["reference_number"]).startswith("REF-10005-")
    assert [(s.id, s.kind, s.cached) for s in done.steps] == [
        ("lookup", "success", False),
        ("open", "success", False),
    ]
    assert commits() == before + 1

    again = go(inputs, "wf-it-1")
    assert again.cached and again.outputs == done.outputs
    assert commits() == before + 1

    unknown = go({"member_id": "99999", "initial_deposit": "125.00"}, "wf-it-2")
    assert (unknown.kind, unknown.code, unknown.step_id) == (
        "business_outcome",
        "NOT_FOUND",
        "lookup",
    )
    assert [s.kind for s in unknown.steps] == ["business_outcome", "not_run"]
    assert commits() == before + 1
