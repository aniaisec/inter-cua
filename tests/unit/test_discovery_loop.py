"""The discovery loop, driven by a scripted model over a fake surface.

No browser and no API key: the loop only ever sees observations and tool
calls, so every way a run can end is reachable from here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cua.agent.goal import Goal, OutputSpec, ParamSpec
from cua.agent.llm import (
    Decision,
    DecisionRequest,
    Provider,
    ScriptedClient,
    ToolCall,
    ToolResultTurn,
)
from cua.agent.loop import DiscoveryConfig, DiscoveryLoop, DiscoveryOutcome
from cua.agent.script import Script, ScriptStep, load_script
from cua.agent.stopping import StopLimits
from cua.evidence.logger import RunLog
from cua.policy.allowlist import Policy, load_policy
from cua.secrets.resolver import Credential
from cua.surface.locators import NearText, RoleName, TableCell, Within
from cua.surface.protocol import Click, Observation, TypeText
from cua.tenant import SecretBinding, Tenant
from tests.unit import screens
from tests.unit.fakes import FakeSurface

REPO = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8000"
USERNAME, PASSWORD = "opuser", "hunter2pw"

TENANT = Tenant(
    id="local",
    app_family="legacy-core",
    base_url=BASE,
    secrets={"mockcore/operator": SecretBinding(var="UNUSED", format="username:password")},
)
POLICY = load_policy(REPO / "policies" / "default.yaml", TENANT)
CREDENTIAL = Credential(
    "secret://local/mockcore/operator", {"username": USERNAME, "password": PASSWORD}
)
GOAL = Goal(
    goal="Look up a member by id and return the current savings balance",
    name="member_savings_balance",
    entry="/login",
    params=[ParamSpec(name="member_id", value="10003")],
    outputs=[
        OutputSpec(name="savings_balance", type="decimal"),
        OutputSpec(name="member_name", optional=True),
    ],
    credentials={"app_login": CREDENTIAL.ref},
)


def _typed(snapshot: str, label: str, value: str) -> str:
    field = f'- cell "{label}"\n      - cell:\n        - textbox\n'
    assert field in snapshot
    return snapshot.replace(field, field.rstrip("\n") + f": {value}\n")


LOGIN_USER = _typed(screens.LOGIN, "User ID", USERNAME)
LOGIN_TYPED = _typed(LOGIN_USER, "Password", PASSWORD)
"""The sign-on screen after both fields were filled. A password field's value
comes back in the accessibility tree like any other; the loop must scrub it."""

REVIEW = """
- heading "Review Sub-account" [level=3]
- table:
  - rowgroup:
    - row "Confirm":
      - cell "Confirm":
        - button "Confirm"
"""

MAIN = Within(frame="main")
USER_ID = [NearText(text="User ID", role="textbox")]
PASSWORD_FIELD = [NearText(text="Password", role="textbox")]
SIGN_ON = [RoleName(role="button", name="Sign On")]
MEMBER_ID = [NearText(text="Member ID", role="textbox", within=MAIN)]
SEARCH = [RoleName(role="button", name="Search", within=MAIN)]
SAVINGS = [TableCell(row_contains="Savings", column_header="Balance", within=MAIN)]
NAME = [NearText(text="Name", role="cell", direction="right", within=MAIN)]


def login(snapshot: str = screens.LOGIN) -> Observation:
    return screens.build({"": snapshot}, urls={"": f"{BASE}/login"})


def framed(main: str, path: str) -> Observation:
    return screens.build(
        {"nav": screens.NAV, "main": main},
        urls={"nav": f"{BASE}/nav", "main": f"{BASE}{path}"},
        location=f"{BASE}/",
        frame_x={"nav": 10.0, "main": 200.0},
    )


def happy_screens() -> list[Observation]:
    return [
        login(),
        login(LOGIN_USER),
        login(LOGIN_TYPED),
        framed(screens.SEARCH, "/search"),
        framed(screens.SEARCH, "/search"),
        framed(screens.MEMBER_DETAIL, "/member/10003"),
    ]


HAPPY = [
    ScriptStep(tool="type", target=USER_ID, text="${credentials.app_login.username}"),
    ScriptStep(tool="type", target=PASSWORD_FIELD, text="${credentials.app_login.password}"),
    ScriptStep(tool="click", target=SIGN_ON),
    ScriptStep(tool="type", target=MEMBER_ID, text="10003"),
    ScriptStep(tool="click", target=SEARCH),
    ScriptStep(tool="done", outputs={"savings_balance": SAVINGS, "member_name": NAME}),
]


class Recorder:
    """Wraps a client, keeping every request so a test can read what the model saw."""

    def __init__(self, inner: ScriptedClient) -> None:
        self.inner = inner
        self.requests: list[DecisionRequest] = []

    @property
    def provider(self) -> Provider:
        return self.inner.provider

    @property
    def model(self) -> str:
        return self.inner.model

    def decide(self, request: DecisionRequest) -> Decision:
        self.requests.append(request)
        return self.inner.decide(request)

    @property
    def seen(self) -> str:
        """Everything that went to the model, as one string."""
        last = self.requests[-1]
        return last.system + "".join(t.model_dump_json(exclude={"png"}) for t in last.transcript)


def run(
    tmp_path: Path,
    steps: list[ScriptStep],
    surface: FakeSurface,
    *,
    policy: Policy = POLICY,
    goal: Goal = GOAL,
    **config: object,
) -> tuple[DiscoveryOutcome, RunLog, Recorder]:
    log = RunLog(tmp_path / "run")
    llm = Recorder(ScriptedClient(Script(steps=steps)))
    outcome = DiscoveryLoop(
        surface=surface,
        llm=llm,
        goal=goal,
        tenant=TENANT,
        policy=policy,
        credentials={"app_login": CREDENTIAL},
        log=log,
        config=DiscoveryConfig.model_validate(config),
    ).run()
    return outcome, log, llm


def events(log: RunLog) -> list[dict[str, object]]:
    return [json.loads(line) for line in (log.dir / "log.jsonl").read_text().splitlines()]


def named(log: RunLog, kind: str) -> list[dict[str, object]]:
    return [e for e in events(log) if e["event"] == kind]


def run_files(log: RunLog) -> str:
    return "".join(
        p.read_text(encoding="utf-8", errors="replace") for p in log.dir.rglob("*") if p.is_file()
    )


# -- reaching the goal ---------------------------------------------------------


def test_a_scripted_run_reaches_done_with_outputs_the_runner_read_itself(tmp_path: Path) -> None:
    surface = FakeSurface(happy_screens())
    outcome, _, _ = run(tmp_path, HAPPY, surface)

    assert outcome.kind == "done", outcome.message
    assert outcome.exit_code == 0
    assert outcome.outputs["savings_balance"].value == "$1,411.21"
    assert outcome.outputs["savings_balance"].normalized == "1411.21"
    assert outcome.outputs["member_name"].normalized == "Test Member 03"
    assert outcome.steps == 6
    # The values came from reads the runner made, not from the model.
    assert surface.acted[-2:] == [
        f"read:{outcome.outputs['savings_balance'].ref}",
        f"read:{outcome.outputs['member_name'].ref}",
    ]


def test_every_decision_is_logged_with_its_reason_and_every_model_call_with_its_id(
    tmp_path: Path,
) -> None:
    _, log, _ = run(tmp_path, HAPPY, FakeSurface(happy_screens()))

    decisions = named(log, "agent.decision")
    assert [d["tool"] for d in decisions] == ["type", "type", "click", "type", "click", "done"]
    assert all(d["reason"] for d in decisions)
    calls = [json.loads(line) for line in (log.dir / "model_calls.jsonl").read_text().splitlines()]
    assert [c["response_id"] for c in calls] == [f"scripted_{i:04d}" for i in range(1, 7)]
    run_json = json.loads((log.dir / "run.json").read_text())
    assert run_json["recording_env"] == {"viewport": {"w": 1280, "h": 800}, "dpr": 1.0}
    assert (log.dir / "screenshots" / "0000.png").exists()


def test_the_exported_script_repeats_the_run_on_fresh_refs(tmp_path: Path) -> None:
    first, log, _ = run(tmp_path / "a", HAPPY, FakeSurface(happy_screens()))
    exported = load_script(log.dir / "script.yaml")

    again, _, _ = run(tmp_path / "b", list(exported.steps), FakeSurface(happy_screens()))

    assert again.kind == "done"
    assert {n: o.normalized for n, o in again.outputs.items()} == {
        n: o.normalized for n, o in first.outputs.items()
    }
    # Outputs are named by where they sit, never by the value they held.
    done = exported.steps[-1]
    assert done.outputs["savings_balance"][0].strategy == "table_cell"
    assert all("1,411" not in r.model_dump_json() for r in done.outputs["savings_balance"][:1])


# -- secrets -------------------------------------------------------------------


def test_credentials_reach_the_browser_but_never_the_model_or_the_log(tmp_path: Path) -> None:
    # The raw screen does carry the password, as the real one does.
    assert PASSWORD in login(LOGIN_TYPED).compact()
    surface = FakeSurface(happy_screens())
    outcome, log, llm = run(tmp_path, HAPPY, surface)

    assert outcome.kind == "done"
    typed = [a for a in surface.actions if isinstance(a, TypeText)]
    assert [t.text for t in typed[:2]] == [USERNAME, PASSWORD]
    for secret in (USERNAME, PASSWORD):
        assert secret not in llm.seen
        assert secret not in run_files(log)
    assert "${credentials.app_login.password}" in llm.seen


def test_an_unknown_placeholder_is_refused_not_typed(tmp_path: Path) -> None:
    steps = [
        ScriptStep(tool="type", target=USER_ID, text="${credentials.app_login.pin}"),
        ScriptStep(tool="stuck"),
    ]
    surface = FakeSurface([login()])
    outcome, _, llm = run(tmp_path, steps, surface)

    assert outcome.reason == "STUCK"
    assert surface.acted == []
    assert "no credential field app_login.pin" in llm.seen


# -- done ----------------------------------------------------------------------


def test_done_missing_a_declared_output_is_rejected_and_the_agent_told_why(
    tmp_path: Path,
) -> None:
    detail = framed(screens.MEMBER_DETAIL, "/member/10003")
    steps = [
        ScriptStep(tool="done", outputs={"member_name": NAME}),
        ScriptStep(tool="done", outputs={"savings_balance": SAVINGS}),
    ]
    outcome, log, llm = run(tmp_path, steps, FakeSurface([detail]))

    rejected = named(log, "agent.done_rejected")
    assert len(rejected) == 1
    assert rejected[0]["problems"] == ["'savings_balance' is missing"]
    told = llm.requests[1].transcript[-1]
    assert isinstance(told, ToolResultTurn)
    assert told.is_error
    assert "savings_balance' is missing" in told.text
    assert outcome.kind == "done"
    assert set(outcome.outputs) == {"savings_balance"}


def test_done_naming_a_value_of_the_wrong_type_is_rejected(tmp_path: Path) -> None:
    detail = framed(screens.MEMBER_DETAIL, "/member/10003")
    steps = [ScriptStep(tool="done", outputs={"savings_balance": NAME})]
    outcome, log, _ = run(tmp_path, steps, FakeSurface([detail]))

    assert "is not a decimal" in named(log, "agent.done_rejected")[0]["problems"][0]
    assert outcome.reason == "STUCK"  # the script ran out; nothing was recorded as done
    assert not (log.dir / "script.yaml").exists()


# -- stopping ------------------------------------------------------------------


def test_a_screen_that_never_changes_is_a_dead_end_and_the_model_is_not_called_again(
    tmp_path: Path,
) -> None:
    search = framed(screens.SEARCH, "/search")
    steps = [ScriptStep(tool="click", target=SEARCH)] * 10
    outcome, log, llm = run(tmp_path, steps, FakeSurface([search]))

    assert outcome.kind == "escalated"
    assert outcome.reason == "DEAD_END"
    assert outcome.exit_code == 3
    assert len(llm.requests) == 3
    assert named(log, "escalation.requested")[0]["reason_code"] == "DEAD_END"


def test_reading_several_values_off_one_screen_is_not_a_dead_end(tmp_path: Path) -> None:
    detail = framed(screens.MEMBER_DETAIL, "/member/10003")
    steps = [ScriptStep(tool="read", target=SAVINGS)] * 4 + [
        ScriptStep(tool="done", outputs={"savings_balance": SAVINGS})
    ]
    outcome, _, _ = run(tmp_path, steps, FakeSurface([detail]))
    assert outcome.kind == "done"


def test_the_step_limit_stops_the_run(tmp_path: Path) -> None:
    screens_ = [framed(screens.SEARCH, "/search"), framed(screens.MEMBER_DETAIL, "/member/1")] * 5
    steps = [ScriptStep(tool="click", target=[RoleName(role="link", name="Member Search")])] * 9
    outcome, _, llm = run(tmp_path, steps, FakeSurface(screens_), limits=StopLimits(max_steps=4))
    assert (outcome.kind, outcome.reason) == ("stopped", "MAX_STEPS")
    assert len(llm.requests) == 4


def test_stuck_raises_an_escalation_with_the_agents_reason(tmp_path: Path) -> None:
    steps = [ScriptStep(tool="stuck", reason="Member 10003 is not in the search results")]
    outcome, log, _ = run(tmp_path, steps, FakeSurface([framed(screens.SEARCH, "/search")]))

    assert (outcome.kind, outcome.reason) == ("escalated", "STUCK")
    request = named(log, "escalation.requested")[0]
    assert request["message"] == "Member 10003 is not in the search results"
    assert request["location"] == f"{BASE}/"


# -- policy --------------------------------------------------------------------


def test_a_risky_action_without_approval_escalates_before_it_happens(tmp_path: Path) -> None:
    review = framed(REVIEW, "/review/10003")
    steps = [ScriptStep(tool="click", target=[RoleName(role="button", name="Confirm")])]
    surface = FakeSurface([review])
    outcome, log, _ = run(tmp_path, steps, surface)

    assert (outcome.kind, outcome.reason) == ("escalated", "NEEDS_APPROVAL")
    assert surface.acted == []
    assert named(log, "escalation.requested")[0]["rule"] == "commit_on_review"


def test_auto_approve_lets_a_risky_action_through_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    review = framed(REVIEW, "/review/10003")
    steps = [ScriptStep(tool="click", target=[RoleName(role="button", name="Confirm")])]
    surface = FakeSurface([review])
    _, log, _ = run(tmp_path, steps, surface, auto_approve_risky=True)

    assert [type(a) for a in surface.actions[1:]] == [Click]
    assert named(log, "policy.auto_approved")[0]["rule"] == "commit_on_review"
    assert "WARNING: auto-approving" in capsys.readouterr().err
    assert json.loads((log.dir / "run.json").read_text())["auto_approve_risky"] is True


def test_the_same_button_elsewhere_is_not_risky(tmp_path: Path) -> None:
    elsewhere = framed(REVIEW, "/help/confirming")
    steps = [
        ScriptStep(tool="click", target=[RoleName(role="button", name="Confirm")]),
        ScriptStep(tool="stuck"),
    ]
    surface = FakeSurface([elsewhere])
    run(tmp_path, steps, surface)
    assert len(surface.acted) == 1


def test_a_screen_outside_the_allowlist_blocks_the_action_and_the_run_goes_on(
    tmp_path: Path,
) -> None:
    offsite = screens.build({"": screens.SEARCH}, urls={"": "https://elsewhere.example/search"})
    steps = [ScriptStep(tool="click", target=[RoleName(role="button", name="Search")])]
    surface = FakeSurface([offsite])
    outcome, log, llm = run(tmp_path, steps, surface)

    assert surface.acted == []
    assert "outside the allowed origins" in named(log, "policy.block")[0]["reason"]
    # The agent was told, and got another turn (the script then ran out).
    assert "Blocked by policy" in llm.seen
    assert outcome.reason == "STUCK"


# -- a model that misbehaves ---------------------------------------------------


class Sloppy:
    """A model that first calls a tool with the wrong arguments, then says
    nothing, then gives up properly."""

    provider = "scripted"
    model = "sloppy"

    def __init__(self) -> None:
        self.turn = 0

    def decide(self, request: DecisionRequest) -> Decision:
        self.turn += 1
        if self.turn == 1:
            call = ToolCall(id="t1", name="click", input={"reason": "no ref"})
            return Decision(tool_call=call, response_id="r1", model="m")
        if self.turn == 2:
            return Decision(tool_call=None, text="Thinking about it.", response_id="r2", model="m")
        call = ToolCall(id="t3", name="stuck", input={"reason": "giving up"})
        return Decision(tool_call=call, response_id="r3", model="m")


def test_a_malformed_call_and_a_turn_without_a_call_are_answered_not_fatal(
    tmp_path: Path,
) -> None:
    log = RunLog(tmp_path / "run")
    sloppy = Sloppy()
    outcome = DiscoveryLoop(
        surface=FakeSurface([login()]),
        llm=sloppy,
        goal=GOAL,
        tenant=TENANT,
        policy=POLICY,
        credentials={"app_login": CREDENTIAL},
        log=log,
    ).run()

    assert outcome.reason == "STUCK"
    assert "ref: Field required" in named(log, "agent.bad_call")[0]["error"]
    assert len(named(log, "agent.no_tool")) == 1
