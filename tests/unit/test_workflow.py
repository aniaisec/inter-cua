"""Workflows: static checks, and every step's safety contract kept.

The runner is exercised with a stand-in for replay that answers per
capability and keeps replay's idempotency cache, so what is under test is
the composition: what is refused before any step, what each step is called
with, where the workflow stops, and what a retry does and does not run
again. The real replay is exercised against the mock app in
``tests/integration/test_workflow.py``.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from cua.cli import main
from cua.policy import tokens
from cua.policy.allowlist import Policy, load_policy
from cua.registry.store import Registry
from cua.replay.invocation import ApprovalGrant, Budget, Invocation
from cua.replay.result import (
    BusinessOutcome,
    Escalated,
    Evidence,
    Failure,
    IdempotencyCache,
    ReplayResult,
    Success,
    fingerprint,
)
from cua.replay.runner import InvocationError
from cua.tenant import SecretBinding, Tenant
from cua.workflow import validator
from cua.workflow.journal import Journal, step_key
from cua.workflow.models import Workflow, WorkflowError, load_workflow, parse_ref
from cua.workflow.planner import plan, static_inputs
from cua.workflow.runner import WorkflowRequest, run
from tests.conftest import REPO_ROOT

SHIPPED = REPO_ROOT / "workflows" / "open_member_subaccount.yaml"
KEY_VAR = "CUA_TEST_SIGNING_KEY"
ENV = {KEY_VAR: "k" * 64}
INPUTS = {"member_id": "10003", "initial_deposit": "250.00"}


@pytest.fixture
def caps(tmp_path: Path) -> Path:
    out = tmp_path / "capabilities"
    shutil.copytree(REPO_ROOT / "capabilities", out)
    return out


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        id="local",
        app_family="legacy-core",
        base_url="http://127.0.0.1:1",
        secrets={"cua/approval-signing-key": SecretBinding(var=KEY_VAR)},
    )


@pytest.fixture
def policy(tenant: Tenant) -> Policy:
    return load_policy(REPO_ROOT / "policies" / "default.yaml", tenant)


def token_for(
    caps: Path, tenant: Tenant, inputs: dict[str, str], name: str = "open_subaccount"
) -> str:
    cap = Registry(caps).version(name, 3).capability
    return tokens.mint(cap, tenant, inputs, approved_by="anil", key=ENV[KEY_VAR].encode())


class FakeReplay:
    """Replay's shape: a run directory per run, and replay's own idempotency
    cache in front, so a retried step is answered without executing."""

    def __init__(self, runs: Path, answers: dict[str, Any] | None = None) -> None:
        self.runs = runs
        self.answers = answers or {}
        self.calls: list[tuple[str, Invocation]] = []
        self.executed: list[str] = []

    def __call__(self, path: Path, *, invocation: Invocation, **kwargs: Any) -> ReplayResult:
        from cua.artifact.store import open_capability

        cap = open_capability(path).capability
        self.calls.append((cap.name, invocation))
        cache = IdempotencyCache(self.runs)
        request = fingerprint(cap.name, cap.version, invocation.inputs)
        if invocation.idempotency_key:
            cached = cache.get(invocation.idempotency_key, request)
            if cached is not None:
                return cached
        self.executed.append(cap.name)
        run_id = f"run_{len(self.executed):04d}_{cap.name}"
        run_dir = self.runs / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps({"request": {"idempotency_key": invocation.idempotency_key}})
        )
        answer = self.answers.get(cap.name, "success")
        common = {
            "capability": cap.name,
            "capability_version": cap.version,
            "run_id": run_id,
            "idempotency_key": invocation.idempotency_key,
            "evidence": Evidence(run_dir=run_dir.as_posix()),
        }
        result: ReplayResult
        if callable(answer):
            result = answer(run_dir, common)
        elif answer == "success" and cap.name == "member_savings_balance":
            result = Success(
                outputs={"savings_balance": "1411.21", "member_name": "Test Member 03"}, **common
            )
        elif answer == "success":
            result = Success(
                outputs={"reference_number": "REF-10003-0001"}, side_effect="committed", **common
            )
        elif answer == "not_found":
            result = BusinessOutcome(code="NOT_FOUND", message="No matching member", **common)
        elif answer == "escalated":
            result = Escalated(
                reason="NEEDS_APPROVAL",
                step_id="review.submit",
                request_id="req_1",
                resume_token="rt_1",
                **common,
            )
        else:
            raise AssertionError(answer)
        (run_dir / "result.json").write_text(result.model_dump_json())
        if invocation.idempotency_key and result.kind != "escalated":
            cache.put(invocation.idempotency_key, request, result)
        return result


def go(
    caps: Path,
    tenant: Tenant,
    policy: Policy,
    fake: FakeReplay,
    *,
    workflow: Path = SHIPPED,
    inputs: dict[str, str] = INPUTS,
    key: str | None = "req-1",
    approvals: dict[str, str] | None = None,
    budget: Budget | None = None,
    handoff: Any = None,
) -> Any:
    request = WorkflowRequest(
        inputs=inputs,
        idempotency_key=key,
        approvals={k: ApprovalGrant(token=v) for k, v in (approvals or {}).items()},
        budget=budget or Budget(),
    )
    return run(
        workflow,
        tenant=tenant,
        policy=policy,
        request=request,
        capabilities_dir=caps,
        runs_dir=fake.runs,
        environ=ENV,
        handoff=handoff,
        step_runner=fake,
    )


def write(tmp_path: Path, data: dict[str, Any], name: str = "wf.yaml") -> Path:
    import yaml

    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def base(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "test_flow",
        "inputs": {"member_id": {"type": "string"}, "initial_deposit": {"type": "decimal"}},
        "steps": [
            {
                "id": "lookup",
                "capability": "member_savings_balance",
                "inputs": {"member_id": "${member_id}"},
            },
            {
                "id": "open",
                "capability": "open_subaccount",
                "inputs": {"member_id": "${member_id}", "initial_deposit": "${initial_deposit}"},
            },
        ],
        "outputs": {"reference_number": "${open.output.reference_number}"},
    }
    data.update(changes)
    return data


def problems_of(caps: Path, data: dict[str, Any]) -> list[str]:
    return validator.problems(plan(Workflow.model_validate(data), Registry(caps)))


# -- definition and static checks ---------------------------------------------------------


def test_the_shipped_workflow_plans_and_checks_clean(caps: Path, tenant: Tenant) -> None:
    p = plan(load_workflow(SHIPPED), Registry(caps))
    assert [(s.id, s.capability.name, s.version) for s in p.steps] == [
        ("lookup", "member_savings_balance", 3),
        ("open", "open_subaccount", 3),
    ]
    assert validator.problems(p) == [] and validator.refusals(p, tenant) == []
    assert [s.needs_consent for s in p.steps] == [False, True]
    assert not p.idempotent
    assert validator.output_types(p) == {
        "member_name": ("string", True),
        "savings_balance_before": ("decimal", False),
        "reference_number": ("string", False),
    }


def test_bindings_parse_to_exactly_one_kind() -> None:
    assert parse_ref("${member_id}").kind == "input"
    ref = parse_ref("${lookup.output.savings_balance}")
    assert (ref.kind, ref.step, ref.name) == ("output", "lookup", "savings_balance")
    assert parse_ref("250.00").kind == "literal"
    for bad in ("id-${member_id}", "${lookup.outputs.x}", "${Member}", "${a.b}"):
        with pytest.raises(ValueError, match="not a binding"):
            parse_ref(bad)


@pytest.mark.parametrize(
    ("step", "inputs", "replace", "expected"),
    [
        ("lookup", {"member_id": "${memberid}"}, False, "${memberid} is not a workflow input"),
        ("lookup", {"member_id": "${nope.output.x}"}, False, "names no step"),
        ("lookup", {"member_id": "${open.output.reference_number}"}, False, "before step 'open'"),
        ("lookup", {"member_id": "${lookup.output.member_name}"}, False, "before step 'lookup'"),
        ("open", {"initial_deposit": "${lookup.output.balance}"}, False, "no output 'balance'"),
        (
            "open",
            {"member_id": "${lookup.output.savings_balance}"},
            False,
            "is string; ${lookup.output.savings_balance} is decimal",
        ),
        ("open", {"member_id": "${lookup.output.member_name}"}, False, "optional and may not"),
        ("open", {"member_id": "${member_id}"}, True, "'initial_deposit' is not bound"),
        ("lookup", {"branch": "x"}, False, "'branch' is not an input"),
        ("lookup", {"member_id": "abc"}, False, "does not match"),
        ("lookup", {"member_id": "id-${member_id}"}, False, "not a binding"),
    ],
)
def test_a_wrong_binding_is_found_before_anything_runs(
    caps: Path, step: str, inputs: dict[str, str], replace: bool, expected: str
) -> None:
    data = base()
    for s in data["steps"]:
        if s["id"] == step:
            s["inputs"] = inputs if replace else {**s["inputs"], **inputs}
    found = problems_of(caps, data)
    assert any(expected in p for p in found), found


def test_workflow_level_mistakes_are_found(caps: Path) -> None:
    data = base(
        inputs={
            "member_id": {"type": "string"},
            "initial_deposit": {"type": "string"},
            "note": {"type": "string"},
        },
        outputs={"ref": "${open.output.reference_number}", "fixed": "x"},
    )
    data["steps"].append(dict(data["steps"][0]))
    found = "\n".join(problems_of(caps, data))
    assert "step id 'lookup' is used more than once" in found
    assert "input 'initial_deposit' is decimal; ${initial_deposit} is string" in found
    assert "workflow input 'note' is declared and never used" in found
    assert "output 'fixed': a workflow output is read from a step or an input" in found


def test_optional_and_sensitive_workflow_inputs_must_fit_where_they_go(caps: Path) -> None:
    data = base(
        inputs={
            "member_id": {"type": "string", "required": False},
            "initial_deposit": {"type": "decimal", "sensitive": True},
        }
    )
    found = "\n".join(problems_of(caps, data))
    assert "input 'member_id' is required; ${member_id} is optional" in found
    assert "not sensitive: the step would log ${initial_deposit} in the clear" in found


def test_an_integer_flows_into_a_decimal_and_an_output_into_a_later_input(caps: Path) -> None:
    data = base(
        inputs={"member_id": {"type": "string"}},
        steps=[
            base()["steps"][0],
            {
                "id": "open",
                "capability": "open_subaccount",
                "inputs": {
                    "member_id": "${member_id}",
                    "initial_deposit": "${lookup.output.savings_balance}",
                },
            },
        ],
    )
    assert problems_of(caps, data) == []
    assert validator.assignable("integer", "decimal")
    assert not validator.assignable("decimal", "integer")
    assert not validator.assignable("decimal", "string")


def test_an_unknown_capability_or_version_cannot_be_planned(caps: Path) -> None:
    data = base()
    data["steps"][1]["version"] = 9
    with pytest.raises(WorkflowError, match="step 'open'"):
        plan(Workflow.model_validate(data), Registry(caps))
    data["steps"][1] = {"id": "open", "capability": "close_account"}
    with pytest.raises(WorkflowError, match="step 'open'"):
        plan(Workflow.model_validate(data), Registry(caps))


def test_a_malformed_file_is_a_workflow_error(tmp_path: Path) -> None:
    path = write(
        tmp_path, {"name": "x", "steps": [{"id": "a", "capability": "b", "inputs": {"n": 250.0}}]}
    )
    with pytest.raises(WorkflowError, match=r"steps\.0\.inputs\.n"):
        load_workflow(path)
    with pytest.raises(WorkflowError, match="steps"):
        load_workflow(write(tmp_path, {"name": "x", "steps": []}, "empty.yaml"))


# -- running: the happy path --------------------------------------------------------------


def test_each_step_runs_through_replay_with_its_own_key_consent_and_budget(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    fake = FakeReplay(tmp_path / "runs")
    token = token_for(caps, tenant, INPUTS)
    result = go(
        caps,
        tenant,
        policy,
        fake,
        approvals={"open": token},
        budget=Budget(timeout_s=200, max_recoveries=1, allow_escalation=False),
    )

    assert result.kind == "success" and result.side_effect == "committed"
    assert result.outputs == {
        "member_name": "Test Member 03",
        "savings_balance_before": "1411.21",
        "reference_number": "REF-10003-0001",
    }
    (n1, lookup), (n2, opened) = fake.calls
    assert (n1, n2) == ("member_savings_balance", "open_subaccount")
    assert lookup.inputs == {"member_id": "10003"} and opened.inputs == INPUTS
    assert lookup.idempotency_key == step_key("open_member_subaccount", "req-1", "lookup")
    assert opened.idempotency_key == step_key("open_member_subaccount", "req-1", "open")
    # Consent goes to the step it was given for, and to no other.
    assert lookup.approval is None
    assert opened.approval is not None and opened.approval.token == token
    # The workflow's budget is shared out; the per-step limits are passed as given.
    assert opened.budget.timeout_s <= lookup.budget.timeout_s <= 200
    assert (opened.budget.max_recoveries, opened.budget.allow_escalation) == (1, False)
    assert [(s.id, s.kind, s.side_effect) for s in result.steps] == [
        ("lookup", "success", "none"),
        ("open", "success", "committed"),
    ]

    run_dir = Path(result.run_dir)
    assert run_dir.parent.name == "workflows" and not (run_dir / "run.json").exists()
    record = json.loads((run_dir / "workflow.json").read_text())
    assert [s["version"] for s in record["plan"]] == [3, 3]
    assert token not in (run_dir / "workflow.json").read_text()
    assert json.loads((run_dir / "result.json").read_text())["kind"] == "success"
    events = [
        json.loads(line)["event"] for line in (run_dir / "log.jsonl").read_text().splitlines()
    ]
    assert events == ["step.start", "step.end", "step.start", "step.end", "workflow.end"]


def test_an_output_is_bound_into_a_later_step_at_run_time(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    data = base(inputs={"member_id": {"type": "string"}})
    data["steps"][1]["inputs"]["initial_deposit"] = "${lookup.output.savings_balance}"
    path = write(tmp_path, data)
    p = plan(load_workflow(path), Registry(caps))
    assert static_inputs(p.step("open"), {"member_id": "10003"}) is None
    token = token_for(caps, tenant, {"member_id": "10003", "initial_deposit": "1411.21"})
    fake = FakeReplay(tmp_path / "runs")
    result = go(
        caps,
        tenant,
        policy,
        fake,
        workflow=path,
        inputs={"member_id": "10003"},
        approvals={"open": token},
    )
    assert result.kind == "success"
    assert fake.calls[1][1].inputs == {"member_id": "10003", "initial_deposit": "1411.21"}


def test_a_business_outcome_ends_the_workflow_before_the_commit(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    fake = FakeReplay(tmp_path / "runs", {"member_savings_balance": "not_found"})
    result = go(caps, tenant, policy, fake, approvals={"open": token_for(caps, tenant, INPUTS)})
    assert (result.kind, result.code, result.step_id, result.side_effect) == (
        "business_outcome",
        "NOT_FOUND",
        "lookup",
        "none",
    )
    assert [n for n, _ in fake.calls] == ["member_savings_balance"]
    assert [(s.id, s.kind) for s in result.steps] == [
        ("lookup", "business_outcome"),
        ("open", "not_run"),
    ]
    assert result.detail is not None and result.detail.kind == "business_outcome"


def test_the_budget_is_for_the_whole_workflow(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    def slow(run_dir: Path, common: dict[str, Any]) -> ReplayResult:
        time.sleep(0.05)
        return Success(outputs={"savings_balance": "1.00"}, **common)

    fake = FakeReplay(tmp_path / "runs", {"member_savings_balance": slow})
    result = go(
        caps,
        tenant,
        policy,
        fake,
        approvals={"open": token_for(caps, tenant, INPUTS)},
        budget=Budget(timeout_s=0.01),
    )
    assert (result.kind, result.code, result.step_id, result.side_effect) == (
        "failure",
        "TIMEOUT",
        "open",
        "none",
    )
    assert [n for n, _ in fake.calls] == ["member_savings_balance"]


# -- refused before any step ----------------------------------------------------------------


def refused(result: Any, code: str, fake: FakeReplay, match: str) -> None:
    assert (result.kind, result.code, result.side_effect) == ("failure", code, "none")
    assert match in result.message, result.message
    assert fake.calls == [] and result.run_dir is None


def test_a_committing_workflow_needs_an_idempotency_key(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    fake = FakeReplay(tmp_path / "runs")
    token = token_for(caps, tenant, INPUTS)
    refused(
        go(caps, tenant, policy, fake, key=None, approvals={"open": token}),
        "INPUT_INVALID",
        fake,
        "pass an idempotency key",
    )


def test_a_read_only_workflow_needs_no_key_and_no_consent(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    data = base(inputs={"member_id": {"type": "string"}}, outputs={})
    data["steps"] = [data["steps"][0], {**data["steps"][0], "id": "again"}]
    fake = FakeReplay(tmp_path / "runs")
    result = go(
        caps,
        tenant,
        policy,
        fake,
        workflow=write(tmp_path, data),
        inputs={"member_id": "10003"},
        key=None,
    )
    assert result.kind == "success" and len(fake.calls) == 2
    assert all(inv.idempotency_key is None for _, inv in fake.calls)


def test_a_commit_without_consent_refuses_the_workflow_before_the_lookup(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    fake = FakeReplay(tmp_path / "runs")
    refused(
        go(caps, tenant, policy, fake),
        "POLICY_BLOCKED",
        fake,
        "step 'open' (open_subaccount) commits a change and needs consent",
    )


def test_with_a_handoff_channel_the_consent_is_left_to_a_person(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    from cua.escalation.channel import HandoffSettings

    fake = FakeReplay(tmp_path / "runs", {"open_subaccount": "escalated"})
    result = go(caps, tenant, policy, fake, handoff=HandoffSettings(wait_s=0))
    assert (result.kind, result.code, result.step_id) == ("escalated", "NEEDS_APPROVAL", "open")
    assert result.resume_token == "rt_1" and "cua resume" in result.message


@pytest.mark.parametrize(
    ("approvals", "match"),
    [
        ({"lookup": "x"}, "step 'lookup' commits nothing"),
        ({"close": "x"}, "'close', which is no step"),
        ({"open": "not-a-token"}, "approval for step 'open' refused: not an approval token"),
    ],
)
def test_misdirected_or_malformed_consent_is_refused(
    caps: Path,
    tenant: Tenant,
    policy: Policy,
    tmp_path: Path,
    approvals: dict[str, str],
    match: str,
) -> None:
    fake = FakeReplay(tmp_path / "runs")
    result = go(caps, tenant, policy, fake, approvals=approvals)
    assert match in result.message and fake.calls == []


def test_consent_for_other_inputs_or_a_spent_token_refuses_before_the_lookup(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    fake = FakeReplay(tmp_path / "runs")
    other = token_for(caps, tenant, {**INPUTS, "initial_deposit": "99.00"})
    refused(
        go(caps, tenant, policy, fake, approvals={"open": other}),
        "POLICY_BLOCKED",
        fake,
        "consent for other inputs",
    )

    token = token_for(caps, tenant, INPUTS)
    approval = tokens.verify(
        token,
        Registry(caps).version("open_subaccount", 3).capability,
        tenant,
        INPUTS,
        key=ENV[KEY_VAR].encode(),
    )
    tokens.SpentTokens(fake.runs).spend(approval, "run_earlier")
    refused(
        go(caps, tenant, policy, fake, key="req-2", approvals={"open": token}),
        "POLICY_BLOCKED",
        fake,
        "already used by run_earlier",
    )


def test_bad_workflow_inputs_are_refused(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    fake = FakeReplay(tmp_path / "runs")
    result = go(caps, tenant, policy, fake, inputs={"member_id": "abc", "branch": "x"})
    refused(result, "INPUT_INVALID", fake, "'branch' is not an input")
    assert "member_id='abc' does not match" in result.message
    assert "'initial_deposit' is required" in result.message


def test_a_revoked_step_or_another_app_family_refuses_every_step(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    fake = FakeReplay(tmp_path / "runs")
    token = token_for(caps, tenant, INPUTS)
    Registry(caps).transition("open_subaccount", 3, "revoke", by="anil", reason="test")
    refused(
        go(caps, tenant, policy, fake, approvals={"open": token}),
        "POLICY_BLOCKED",
        fake,
        "step 'open': open_subaccount v3 is revoked",
    )

    other = tenant.model_copy(update={"app_family": "other-core"})
    result = go(caps, other, policy, fake, key="req-3")
    refused(result, "POLICY_BLOCKED", fake, "step 'lookup': member_savings_balance v3 is for")


# -- retries ----------------------------------------------------------------------------------


def test_a_retry_returns_the_stored_result_and_another_request_under_the_key_is_refused(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    fake = FakeReplay(tmp_path / "runs")
    first = go(caps, tenant, policy, fake, approvals={"open": token_for(caps, tenant, INPUTS)})
    again = go(caps, tenant, policy, fake)
    assert again.cached and again.outputs == first.outputs and len(fake.calls) == 2

    other = go(caps, tenant, policy, fake, inputs={**INPUTS, "initial_deposit": "99.00"})
    refused(other, "INPUT_INVALID", FakeReplay(fake.runs), "already used for a different request")


def test_a_lost_answer_is_rebuilt_from_the_steps_without_committing_again(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    fake = FakeReplay(tmp_path / "runs")
    go(caps, tenant, policy, fake, approvals={"open": token_for(caps, tenant, INPUTS)})
    book = Journal(fake.runs)
    entry = book.get("req-1")
    assert entry is not None
    entry.result = None  # the process died before the workflow's answer was stored
    book.put("req-1", entry)

    # The caller retries with the token it had, expired by now: the commit
    # happened, so it is answered, and the token never reaches the step.
    again = go(caps, tenant, policy, fake, approvals={"open": "cat1.expired.token"})
    assert again.kind == "success" and not again.cached
    assert fake.calls[-1][1].approval is None
    assert fake.executed == ["member_savings_balance", "open_subaccount"]  # nothing twice
    assert [s.cached for s in again.steps] == [True, True]
    assert again.outputs["reference_number"] == "REF-10003-0001"


def test_the_retry_runs_the_versions_the_first_attempt_resolved(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    fake = FakeReplay(tmp_path / "runs", {"open_subaccount": "escalated"})
    go(caps, tenant, policy, fake, approvals={"open": token_for(caps, tenant, INPUTS)})
    entry = Journal(fake.runs).get("req-1")
    assert entry is not None and entry.pins == {"lookup": 3, "open": 3}


def test_an_escalated_step_is_not_started_again_and_its_answer_carries_the_workflow_on(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    from cua.escalation.channel import HandoffSettings

    fake = FakeReplay(tmp_path / "runs", {"open_subaccount": "escalated"})
    handoff = HandoffSettings(wait_s=0)
    first = go(caps, tenant, policy, fake, handoff=handoff)
    assert first.kind == "escalated"

    waiting = go(caps, tenant, policy, fake, handoff=handoff)
    assert waiting.kind == "escalated" and waiting.resume_token == "rt_1"
    assert fake.executed == ["member_savings_balance", "open_subaccount"]

    # `cua resume` finishes the run in its own directory.
    run_dir = Path(first.steps[1].run_dir or "")
    finished = Success(
        capability="open_subaccount",
        capability_version=3,
        outputs={"reference_number": "REF-10003-0001"},
        side_effect="committed",
        run_id=run_dir.name,
        evidence=Evidence(run_dir=run_dir.as_posix()),
    )
    (run_dir / "result.json").write_text(finished.model_dump_json())

    done = go(caps, tenant, policy, fake)
    assert (done.kind, done.side_effect) == ("success", "committed")
    assert done.outputs["reference_number"] == "REF-10003-0001"
    assert fake.executed == ["member_savings_balance", "open_subaccount"]


def test_a_commit_cut_off_mid_run_is_never_started_again(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    def killed(run_dir: Path, common: dict[str, Any]) -> ReplayResult:
        raise KeyboardInterrupt

    token = token_for(caps, tenant, INPUTS)
    fake = FakeReplay(tmp_path / "runs", {"open_subaccount": killed})
    with pytest.raises(KeyboardInterrupt):
        go(caps, tenant, policy, fake, approvals={"open": token})

    retry = FakeReplay(fake.runs)
    result = go(caps, tenant, policy, retry)
    assert (result.kind, result.code, result.step_id, result.side_effect) == (
        "failure",
        "INTERRUPTED",
        "open",
        "unknown",
    )
    assert retry.executed == []

    # When the run did write what it knew, that is the answer.
    [run_dir] = [d for d in fake.runs.glob("run_*_open_subaccount")]
    cut = Failure(
        code="INTERRUPTED",
        side_effect="committed",
        capability="open_subaccount",
        capability_version=3,
        step_id="review.submit",
        evidence=Evidence(run_dir=run_dir.as_posix()),
    )
    (run_dir / "result.json").write_text(cut.model_dump_json())
    book = Journal(fake.runs)
    entry = book.get("req-1")
    assert entry is not None
    entry.result = None
    entry.steps["open"].state = "started"
    book.put("req-1", entry)
    result = go(caps, tenant, policy, retry)
    assert (result.code, result.side_effect) == ("INTERRUPTED", "committed")
    assert retry.executed == []


def test_a_step_that_could_not_start_is_not_left_looking_interrupted(
    caps: Path, tenant: Tenant, policy: Policy, tmp_path: Path
) -> None:
    class Refusing(FakeReplay):
        def __call__(self, path: Path, *, invocation: Invocation, **kwargs: Any) -> ReplayResult:
            if "open_subaccount" in path.as_posix():
                raise InvocationError("credential app_login: not set")
            return super().__call__(path, invocation=invocation, **kwargs)

    token = token_for(caps, tenant, INPUTS)
    fake = Refusing(tmp_path / "runs")
    with pytest.raises(InvocationError):
        go(caps, tenant, policy, fake, approvals={"open": token})
    entry = Journal(fake.runs).get("req-1")
    assert entry is not None and "open" not in entry.steps

    result = go(caps, tenant, policy, FakeReplay(fake.runs), approvals={"open": token})
    assert result.kind == "success"


# -- CLI ----------------------------------------------------------------------------------------


def test_cli_check_and_list(caps: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["workflow", "check", "open_member_subaccount", "--capabilities-dir", str(caps)]) == 0
    )
    out = capsys.readouterr().out
    assert "2. open: open_subaccount v3 (approved)  commits: needs consent" in out
    assert "OK: every reference and type checks" in out
    assert main(["workflow", "list", "--capabilities-dir", str(caps)]) == 0
    assert "open_member_subaccount  v1  ready" in capsys.readouterr().out
    assert main(["workflow", "check", "nope", "--capabilities-dir", str(caps)]) == 64


def test_cli_check_reports_problems(
    caps: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = base()
    data["steps"][1]["inputs"]["member_id"] = "${lookup.output.member_name}"
    path = write(tmp_path, data)
    assert main(["workflow", "check", str(path), "--capabilities-dir", str(caps)]) == 1
    assert "PROBLEM: step 'open'" in capsys.readouterr().out


def test_cli_approval_token_is_consent_for_that_step_and_those_inputs(
    caps: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_file = tmp_path / "tenant.yaml"
    tenant_file.write_text(
        (REPO_ROOT / "tenants" / "local.yaml")
        .read_text()
        .replace(".cua/approval-signing.key", (tmp_path / "signing.key").as_posix())
    )
    argv = [
        "workflow",
        "approval-token",
        "open_member_subaccount",
        "--tenant",
        str(tenant_file),
        "--by",
        "anil",
        "--capabilities-dir",
        str(caps),
        "--input",
        "member_id=10003",
        "--input",
        "initial_deposit=250.00",
    ]
    assert main([*argv, "--step", "open"]) == 0
    token = capsys.readouterr().out.strip()
    from cua.tenant import load_tenant

    t = load_tenant(str(tenant_file))
    cap = Registry(caps).version("open_subaccount", 3).capability
    approval = tokens.verify(token, cap, t, INPUTS, key=tokens.signing_key(t))
    assert approval.approved_by == "anil"

    assert main([*argv, "--step", "lookup"]) == 64
    assert "commits nothing" in capsys.readouterr().err
