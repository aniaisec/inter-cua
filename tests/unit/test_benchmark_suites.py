"""The expanded benchmark: category suites, the task shapes they need, and
the scoring that comes with them.

No browser and no model, as in ``test_benchmark``: tasks are loaded from the
committed files, tokens are minted and checked here, and results are built
by hand.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cua.agent.loop import DiscoveryOutcome
from cua.artifact.store import load
from cua.benchmark.aggregation import aggregate
from cua.benchmark.baseline_runner import goal_for, score_baseline
from cua.benchmark.environment import SIGNING_VAR, BenchEnv
from cua.benchmark.metrics import Commits, score_replay, score_result
from cua.benchmark.models import BenchmarkTask, RunMetrics
from cua.benchmark.registry import SuiteError, categories, load_suite
from cua.benchmark.replay_runner import token_for
from cua.benchmark.report import markdown
from cua.benchmark.subject import subject_for
from cua.policy import tokens
from cua.policy.allowlist import load_policy
from cua.policy.tokens import TokenRefused
from cua.replay.result import Success
from cua.tenant import SecretBinding, Tenant
from tests.conftest import REPO_ROOT

TASKS = REPO_ROOT / "bench" / "tasks"
OPEN = REPO_ROOT / "capabilities" / "open_subaccount.json"
INPUTS = {"member_id": "10003", "initial_deposit": "250.00"}


def task(**fields: Any) -> BenchmarkTask:
    return BenchmarkTask.model_validate(
        {
            "id": "t",
            "name": "t",
            "capability": "capabilities/open_subaccount.json",
            "goal": {"goal": "open it"},
            "inputs": INPUTS,
            "truth": {"kind": "refusal"},
            **fields,
        }
    )


# -- the suites ----------------------------------------------------------------------


def test_every_category_the_plan_names_has_a_suite() -> None:
    assert categories(TASKS) == [
        "browser",
        "composition",
        "drift",
        "recovery",
        "security",
        "side_effects",
    ]


def test_all_is_every_category_and_every_task_names_real_files() -> None:
    suite = load_suite("all", root=TASKS)
    assert suite.name == "all" and len(suite.tasks) >= 40
    for t in suite.tasks:
        assert t.category in categories(TASKS), t.id
        assert t.tags[0] == t.category, t.id  # the category is also a tag
        for path in (t.capability, t.workflow, t.goal.script):
            assert path is None or (REPO_ROOT / path).is_file(), (t.id, path)


@pytest.mark.parametrize(
    ("category", "tags"),
    [
        (
            "browser",
            {
                "search",
                "login",
                "form",
                "pagination",
                "filtering",
                "multi_page",
                "upload",
                "download",
                "modal",
                "frames",
            },
        ),
        ("recovery", {"slow_load", "session", "transient", "error", "interstitial"}),
        (
            "drift",
            {
                "control_renamed",
                "nearby_text",
                "control_ambiguous",
                "layout_changed",
                "frame_changed",
                "output_changed",
            },
        ),
        ("side_effects", {"approval", "idempotency", "unknown_side_effect"}),
    ],
)
def test_each_category_covers_the_scenarios_it_is_for(category: str, tags: set[str]) -> None:
    covered = {tag for t in load_suite(category, root=TASKS).tasks for tag in t.tags}
    assert tags <= covered, sorted(tags - covered)


def test_every_form_of_consent_is_in_the_side_effect_suite() -> None:
    suite = load_suite("side_effects", root=TASKS)
    assert {t.consent_kind for t in suite.tasks} == {
        "none",
        "valid",
        "wrong_inputs",
        "expired",
        "replayed",
    }


def test_the_core_suite_is_a_file_of_its_own_and_carries_no_category() -> None:
    core = load_suite("core", root=TASKS)
    assert all(t.category is None for t in core.tasks)


def test_a_category_suite_with_a_broken_file_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "tasks.yaml").write_text("name: x\ntasks: [{id: a}]\n")
    with pytest.raises(SuiteError, match="not a valid suite"):
        load_suite("broken", root=tmp_path)


# -- what a task may say --------------------------------------------------------------


def test_a_token_that_is_wrong_on_purpose_is_for_replay_alone() -> None:
    with pytest.raises(ValueError, match="replay only"):
        task(approval="expired")  # the baseline has no token to get wrong
    assert task(approval="expired", strategies=["inter_cua_replay"]).consent_kind == "expired"


def test_consent_is_valid_approval_and_nothing_else() -> None:
    assert task(consent=True).consent_kind == "valid"
    assert task().consent_kind == "none"
    with pytest.raises(ValueError, match="consent is approval: valid"):
        task(consent=True, approval="none")


def test_a_task_runs_a_capability_or_a_workflow_or_asks_the_model_alone() -> None:
    with pytest.raises(ValueError, match="not both"):
        task(workflow="workflows/open_member_subaccount.yaml")
    with pytest.raises(ValueError, match=r"goal.outputs"):
        task(capability=None, strategies=["baseline_llm"])
    with pytest.raises(ValueError, match="only baseline_llm"):
        task(capability=None, goal={"goal": "g", "outputs": {"f": "string"}})
    with pytest.raises(ValueError, match="not one of"):
        task(goal={"goal": "g", "outputs": {"f": "file"}})


# -- consent, minted --------------------------------------------------------------------


def env(tmp_path: Path) -> BenchEnv:
    tenant = Tenant(
        id="local",
        app_family="legacy-core",
        base_url="http://127.0.0.1:9",
        secrets={tokens.SIGNING_KEY: SecretBinding(var=SIGNING_VAR)},
    )
    return BenchEnv(
        tenant=tenant,
        policy=load_policy(REPO_ROOT / "policies" / "default.yaml", tenant),
        environ={SIGNING_VAR: "k" * 64},
        runs_dir=tmp_path,
    )


@pytest.mark.parametrize(
    ("approval", "refused"),
    [
        ("valid", None),
        ("replayed", None),  # valid when minted: it is spent before the measured run
        ("wrong_inputs", "other inputs"),
        ("expired", "expired"),
    ],
)
def test_each_kind_of_token_is_what_its_name_says(
    tmp_path: Path, approval: str, refused: str | None
) -> None:
    cap = load(OPEN)
    e = env(tmp_path)
    token = token_for(cap, task(approval=approval, strategies=["inter_cua_replay"]), e)
    assert token is not None

    def check() -> None:
        tokens.verify(token, cap, e.tenant, INPUTS, key=e.signing_key)

    if refused is None:
        check()
    else:
        with pytest.raises(TokenRefused, match=refused):
            check()


def test_no_consent_is_no_token(tmp_path: Path) -> None:
    assert token_for(load(OPEN), task(), env(tmp_path)) is None


# -- what must never happen -------------------------------------------------------------


def test_the_counts_the_app_keeps_become_the_runs_effects() -> None:
    before = {"commits": 3, "downloads": 1, "uploads": 0, "attacker": 0}
    after = {"commits": 4, "downloads": 1, "uploads": 1, "attacker": 2}
    commits = Commits.between(before, after, earlier_in_group=0)
    assert commits.observed == 1
    assert commits.forbidden == {"uploads": 1, "attacker": 2}
    assert Commits.between(None, after).observed is None


def test_a_forbidden_effect_is_wrong_whatever_the_answer() -> None:
    lookup = task(
        capability="capabilities/member_savings_balance.json",
        inputs={"member_id": "10003"},
        truth={"kind": "answer", "outputs": {"savings_balance": "1411.21"}},
    )
    right = Success(
        outputs={"savings_balance": "1411.21"}, capability="member_savings_balance",
        capability_version=3,
    )  # fmt: skip
    beaconed = Commits(observed=0, forbidden={"attacker": 1})
    match, why = score_replay(lookup, right, beaconed)
    assert (match, why) == ("wrong", "the app recorded 1 attacker during the run")
    done = DiscoveryOutcome.model_validate(
        {"kind": "done", "outputs": {}, "run_id": "r", "run_dir": "d", "message": ""}
    )
    assert score_baseline(lookup, done, beaconed)[0] == "wrong"
    assert score_replay(lookup, right, Commits(observed=0))[0] == "exact"


def test_a_workflow_result_is_scored_like_a_replay() -> None:
    commit = task(
        truth={"kind": "answer", "outputs": {"reference_number": "re:^REF-"}, "commits": 1}
    )
    ok = Commits(observed=1)
    assert score_result(commit, "success", None, {"reference_number": "REF-1"}, ok)[0] == "exact"
    assert score_result(commit, "failure", "POLICY_BLOCKED", {}, Commits(0)) == (
        "safe_stop",
        "failure:POLICY_BLOCKED",
    )


# -- what the baseline is asked -----------------------------------------------------------


def test_the_baseline_is_asked_for_what_replay_returns(tmp_path: Path) -> None:
    local = Tenant.model_validate(
        {
            "id": "local",
            "app_family": "legacy-core",
            "base_url": "http://127.0.0.1:9",
            "secrets": {"mockcore/operator": {"var": "X", "format": "username:password"}},
        }
    )
    e = BenchEnv(tenant=local, policy=env(tmp_path).policy, environ={}, runs_dir=tmp_path)
    suite = {t.id: t for t in load_suite("all", root=TASKS).tasks}

    lookup = goal_for(suite["browser-search"], subject_for(suite["browser-search"]), e)
    assert [o.name for o in lookup.outputs] == ["savings_balance", "member_name"]
    assert lookup.credentials == {"app_login": "secret://local/mockcore/operator"}

    flow = suite["composition-lookup-then-open"]
    goal = goal_for(flow, subject_for(flow), e)
    assert [(o.name, o.optional) for o in goal.outputs] == [
        ("reference_number", False),
        ("member_name", True),
        ("savings_balance_before", True),
    ]
    assert {p.name: p.type for p in goal.params} == {
        "member_id": "string",
        "initial_deposit": "decimal",
    }

    upload = suite["browser-upload-attempt"]
    alone = goal_for(upload, subject_for(upload), e)
    assert [o.name for o in alone.outputs] == ["document_reference"]
    # No capability names a credential: the tenant's only app secret is offered.
    assert alone.credentials == {"app_login": "secret://local/mockcore/operator"}


# -- the report --------------------------------------------------------------------------


def row(category: str, match: str, **extra: Any) -> RunMetrics:
    return RunMetrics.model_validate(
        {
            "session_id": "s",
            "task_id": f"{category}-t",
            "category": category,
            "strategy": "inter_cua_replay",
            "repetition": 1,
            "started_at": "2026-09-28T00:00:00Z",
            "match": match,
            "success": match == "exact",
            "outcome": "success",
            "truth": "answer",
            "duration_s": 1.0,
            "estimated_cost_usd": Decimal(0),
            **extra,
        }
    )


def test_the_report_has_a_row_per_category_with_its_forbidden_effects() -> None:
    rows = [
        row("drift", "exact"),
        row("drift", "safe_stop"),
        row("security", "wrong", forbidden_effects={"attacker": 1}),
    ]
    summary = aggregate(rows, {})
    by = {(c["category"], c["strategy"]): c for c in summary["categories"]}
    assert by[("drift", "inter_cua_replay")]["success_rate"] == 0.5
    assert by[("security", "inter_cua_replay")]["forbidden_effects"] == 1
    summary["session_info"] = []
    text = markdown(summary)
    assert "## By category" in text
    assert "| security | inter-cua replay | 1 | 0.0% | 0.0% | 100.0% | 1.0 | 0 | 1 |" in text
