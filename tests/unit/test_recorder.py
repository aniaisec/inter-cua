"""The recorder: a finished discovery run → a draft capability.

The golden test pins the whole artifact for the committed goal-1 run; the
others pin one rule each, on runs made with the scripted model over the fake
surface so every variant is a few lines.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from cua.agent.goal import Goal
from cua.agent.script import ScriptStep
from cua.artifact.recorder import RecordError, record
from cua.artifact.schema import Capability
from cua.surface.conditions import LocationMatches, RegionPresent, TextPresent
from cua.surface.locators import RoleName
from tests.unit import artifacts
from tests.unit.fakes import FakeSurface
from tests.unit.test_discovery_loop import (
    HAPPY,
    MEMBER_ID,
    REVIEW,
    SEARCH,
    framed,
    happy_screens,
)
from tests.unit.test_discovery_loop import (
    run as discover,
)


def recorded(tmp_path: Path, steps: list[ScriptStep], surface: FakeSurface, **kw) -> Capability:
    outcome, log, _ = discover(tmp_path, steps, surface, **kw)
    assert outcome.kind == "done", outcome.message
    return record(log.dir, policy=artifacts.policy(), families_dir=artifacts.FAMILIES)


# -- the golden ----------------------------------------------------------------


def test_the_goal_one_run_records_to_the_golden_capability() -> None:
    cap = artifacts.record_goal1()
    if os.environ.get("CUA_UPDATE_GOLDEN"):
        artifacts.GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        artifacts.GOLDEN.write_text(cap.to_json(), encoding="utf-8", newline="\n")
    assert cap.to_json() == artifacts.GOLDEN.read_text(encoding="utf-8")


def test_the_goal_one_capability_says_what_the_run_did() -> None:
    cap = artifacts.record_goal1()

    assert [s.id for s in cap.steps] == [
        "login.username",
        "login.password",
        "login.submit",
        "search.member_id",
        "search.submit",
    ]
    assert cap.steps[3].value == "${member_id}"
    assert cap.inputs["member_id"].pattern == "^[0-9]+$"
    assert cap.credentials["app_login"].ref == "secret://{tenant.id}/mockcore/operator"
    assert cap.target.entry.pattern == "{tenant.base_url}/login"
    assert cap.provenance.locator_rungs_used["search.submit"] == "role_name"
    assert cap.outputs["savings_balance"].extract.parse == "currency_usd"
    assert cap.approval_state == "draft"
    assert cap.version == 1


def test_no_discovery_value_or_secret_survives_into_the_steps() -> None:
    data = json.loads(artifacts.record_goal1().to_json())
    steps = json.dumps(data["steps"])
    assert "10003" not in steps
    assert "1,411" not in json.dumps(data) and "1411" not in json.dumps(data)
    assert "Test Member" not in json.dumps(data)
    assert "operator" not in json.dumps(data).replace("mockcore/operator", "")


def test_outputs_are_named_by_position_never_by_the_value_read() -> None:
    cap = artifacts.record_goal1()
    savings = cap.outputs["savings_balance"].extract.target
    name = cap.outputs["member_name"].extract.target
    assert savings[0].model_dump(include={"strategy", "row_contains", "column_header"}) == {
        "strategy": "table_cell",
        "row_contains": "Savings",
        "column_header": "Balance",
    }
    assert name[0].model_dump(include={"strategy", "text", "direction"}) == {
        "strategy": "near_text",
        "text": "Name",
        "direction": "right",
    }


def test_checkpoints_bind_the_input_and_tell_detail_from_denied() -> None:
    cp = {c.id: c for c in artifacts.record_goal1().checkpoints}
    assert set(cp) == {"cp.logged_in", "cp.member_detail", "cp.done"}
    assert cp["cp.logged_in"].after_step == "login.submit"

    detail = cp["cp.member_detail"].all_of
    assert any(isinstance(c, LocationMatches) and c.pattern == "/member/[0-9]+$" for c in detail)
    assert TextPresent(text="${member_id}", within={"frame": "main"}) in detail  # type: ignore[arg-type]
    # "Member Detail" is also the title of the not-authorized page; the
    # Balances heading above the output is what only the real one has.
    regions = {c.name for c in detail if isinstance(c, RegionPresent)}
    assert regions == {"Member Detail", "Balances"}


def test_family_detectors_arrive_only_where_their_anchors_exist() -> None:
    cap = artifacts.record_goal1()
    codes = [d.code for d in cap.outcome_detectors]
    assert codes == [
        "APP_ERROR",
        "NOT_FOUND",
        "PERMISSION_DENIED",
        "INTERSTITIAL",
        "SESSION_EXPIRED",
        "SLOW_LOAD",
    ]
    # VALIDATION_ERROR is scoped to subaccount.submit, which this capability lacks.
    assert "VALIDATION_ERROR" not in cap.contract.outcomes
    assert set(cap.contract.outcomes) == {"NOT_FOUND", "PERMISSION_DENIED"}
    assert cap.recoverers["relogin"].sub_flow == [
        "login.username",
        "login.password",
        "login.submit",
    ]


def test_provenance_names_the_model_that_answered_and_hashes_the_transcript(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    shutil.copytree(artifacts.GOAL1_RUN, run_dir)
    run = json.loads((run_dir / "run.json").read_text())
    run.update(provider="gemini", model="gemini-flash-latest")  # the alias asked for
    (run_dir / "run.json").write_text(json.dumps(run))
    calls = [
        json.loads(line) | {"model": "gemini-3.8-flash"}
        for line in (run_dir / "model_calls.jsonl").read_text().splitlines()
    ]
    (run_dir / "model_calls.jsonl").write_text("\n".join(json.dumps(c) for c in calls) + "\n")

    cap = artifacts.record_goal1(run_dir)
    assert (cap.provenance.provider, cap.provenance.model) == ("gemini", "gemini-3.8-flash")

    before = cap.provenance.transcript_sha256
    with (run_dir / "log.jsonl").open("a") as log:
        log.write("\n")
    assert artifacts.record_goal1(run_dir).provenance.transcript_sha256 != before


# -- templating ----------------------------------------------------------------


def test_only_a_value_equal_to_a_declared_param_is_templated(tmp_path: Path) -> None:
    steps = list(HAPPY)
    steps[3] = ScriptStep(tool="type", target=MEMBER_ID, text="10003 ")
    cap = recorded(tmp_path, steps, FakeSurface(happy_screens()))
    assert cap.steps[3].value == "10003 "  # not exactly the param: left alone


def test_a_literal_is_never_templated_without_a_declared_param(tmp_path: Path) -> None:
    from tests.unit.test_discovery_loop import GOAL

    goal = GOAL.model_copy(update={"params": []})
    cap = recorded(tmp_path, HAPPY, FakeSurface(happy_screens()), goal=goal)
    assert cap.steps[3].value == "10003"
    assert cap.inputs == {}
    assert cap.steps[3].id == "search.member_id"  # named by its label instead


def test_a_masked_value_cannot_be_recorded(tmp_path: Path) -> None:
    steps = list(HAPPY)
    steps[1] = ScriptStep(tool="type", target=steps[1].target, text="hunter2pw")
    outcome, log, _ = discover(tmp_path, steps, FakeSurface(happy_screens()))
    assert outcome.kind == "done"
    with pytest.raises(RecordError, match="typed a masked value"):
        record(log.dir, policy=artifacts.policy(), families_dir=artifacts.FAMILIES)


# -- risk ----------------------------------------------------------------------


def test_a_step_the_policy_calls_risky_is_irreversible_and_never_retried(tmp_path: Path) -> None:
    goal = Goal(goal="Confirm the application", name="confirm_application", entry="/login")
    confirmed = framed('- heading "Sub-account opened" [level=3]\n', "/confirm/10003")
    steps = [
        ScriptStep(tool="click", target=[RoleName(role="button", name="Confirm")]),
        ScriptStep(tool="done", outputs={}),
    ]
    surface = FakeSurface([framed(REVIEW, "/review/10003"), confirmed])
    cap = recorded(tmp_path, steps, surface, goal=goal, auto_approve_risky=True)

    (step,) = cap.steps
    assert step.id == "review.submit"
    assert (step.risk, step.approval, step.retry.allowed) == ("irreversible", "required", False)
    assert cap.contract.side_effects == "modifies_record"
    assert (cap.contract.idempotent, cap.contract.may_escalate) == (False, True)


# -- runs that cannot be recorded --------------------------------------------------


def test_a_run_that_did_not_reach_done_is_refused(tmp_path: Path) -> None:
    steps = [ScriptStep(tool="click", target=SEARCH), ScriptStep(tool="stuck", reason="lost")]
    _, log, _ = discover(tmp_path, steps, FakeSurface([framed("", "/search")]))
    with pytest.raises(RecordError, match="ended escalated \\(STUCK\\)"):
        record(log.dir, policy=artifacts.policy(), families_dir=artifacts.FAMILIES)


def test_a_run_without_an_ending_is_refused(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    shutil.copytree(artifacts.GOAL1_RUN, run_dir)
    (run_dir / "result.json").unlink()
    with pytest.raises(RecordError, match="never finished"):
        artifacts.record_goal1(run_dir)


def test_an_unknown_app_family_is_refused(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    shutil.copytree(artifacts.GOAL1_RUN, run_dir)
    run = json.loads((run_dir / "run.json").read_text())
    run["tenant"]["app_family"] = "other-core"
    (run_dir / "run.json").write_text(json.dumps(run))
    with pytest.raises(RecordError, match="no template for app family 'other-core'"):
        artifacts.record_goal1(run_dir)


def test_the_transcript_hash_survives_a_change_of_line_endings(tmp_path: Path) -> None:
    """git may hand the committed log back with other line endings than the
    machine that wrote it; the provenance must still match it."""
    run_dir = tmp_path / "run"
    shutil.copytree(artifacts.GOAL1_RUN, run_dir)
    log = run_dir / "log.jsonl"
    log.write_bytes(log.read_bytes().replace(b"\n", b"\r\n"))
    assert artifacts.record_goal1(run_dir) == artifacts.record_goal1()
