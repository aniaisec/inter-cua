"""The drift lifecycle against the real mock app: an approved capability
breaks on a renamed button, the break becomes a candidate repair, and the
candidate is evaluated beside the version it repairs, while the version that
broke stays approved, stays the default and keeps its files byte for byte.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

import pytest

from cua.drift.candidate import propose
from cua.drift.detect import read_drift
from cua.drift.evaluate import evaluate
from cua.observability.recorder import read_run
from cua.policy.allowlist import load_policy
from cua.registry.resolver import resolve
from cua.registry.store import Registry
from cua.replay.invocation import Invocation
from cua.replay.result import Failure
from cua.replay.runner import replay
from cua.tenant import SecretBinding, Tenant
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.browser

NAME = "member_savings_balance"
ENV = {"CUA_TEST_OPERATOR": "operator:operator"}
T = TypeVar("T")


def apart(fn: Callable[[], T]) -> T:
    """Run on a thread of its own. Replay and evaluation start their own
    Playwright, as the CLI does; the browser tests keep one open on this
    thread for the whole session, and the sync API allows one per thread."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn).result()


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and "candidates" not in p.relative_to(root).parts:
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def test_a_renamed_button_failure_becomes_an_evaluated_candidate_and_nothing_else_changes(
    mockapp_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)  # the benchmark suite names its capabilities from here
    caps = tmp_path / "capabilities"
    shutil.copytree(REPO_ROOT / "capabilities", caps)
    before = tree_hash(caps)
    tenant = Tenant(
        id="local",
        app_family="legacy-core",
        base_url=mockapp_url,
        secrets={
            "mockcore/operator": SecretBinding(var="CUA_TEST_OPERATOR", format="username:password")
        },
    )
    approved = caps / "registry" / NAME / "v3.json"

    broke = apart(
        lambda: replay(
            approved,
            tenant=tenant,
            policy=load_policy(REPO_ROOT / "policies" / "default.yaml", tenant),
            invocation=Invocation(inputs={"member_id": "10003"}, inject="renamed_button"),
            runs_dir=tmp_path / "runs",
            environ=ENV,
        )
    )
    assert isinstance(broke, Failure) and broke.code == "LOCATOR_UNRESOLVED"
    run_dir = Path(broke.evidence.run_dir or "")
    # The engine now logs what each rung found, as data.
    [failed] = read_run(run_dir).of("locator.failed")
    assert failed.attrs["attempts_from"] == "log"
    drift = read_drift(run_dir)
    assert drift is not None and [e.kind for e in drift.events] == ["CONTROL_RENAMED"]

    candidate, _ = propose(run_dir, capabilities_dir=caps, state_dir=tmp_path / "state")
    assert candidate.record.version == 4
    assert candidate.record.change.added[0]["name"] == "Find"

    done = apart(
        lambda: evaluate(
            candidate,
            capabilities_dir=caps,
            task_ids=["lookup-success", "lookup-renamed-button"],
            runs_root=tmp_path / "eval",
        )
    )
    ev = done.record.evaluation
    assert ev is not None and ev.passed, [g for g in ev.gates if not g.passed] if ev else None
    by_task = {t.task_id: t for t in ev.tasks}
    assert by_task["lookup-renamed-button"].reproduces_drift
    assert by_task["lookup-renamed-button"].incumbent.exact == 0
    assert by_task["lookup-renamed-button"].candidate.exact == 1
    assert by_task["lookup-success"].candidate.exact == 1  # the old screen is still served

    assert tree_hash(caps) == before
    assert resolve(Registry(caps, state_dir=tmp_path / "state"), NAME).record.version == 3
