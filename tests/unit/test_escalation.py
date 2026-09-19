"""Escalation and handoff, without a browser.

The state machine and its file, the lease, the clock that stops while a person
has the controls, the file queue and the channel over it, the operator
console's decisions — and the engine's side of a handoff, driven through an
in-memory channel over a fake surface: what it asks, what it offers, and what
it does with each answer. Above all: the screen decides where the run carries
on, and a commit is never made twice.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from itertools import product
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cua.agent.llm import ScriptedClient
from cua.agent.loop import DiscoveryConfig, DiscoveryLoop
from cua.agent.script import Script, ScriptStep
from cua.artifact.recorder import RecordError, record
from cua.artifact.schema import Capability
from cua.escalation.channel import (
    HUMAN_ACTIONS_FILE,
    HandoffSettings,
    OperatorChannel,
    count_human_actions,
)
from cua.escalation.controller import (
    TRANSITIONS,
    ControlStore,
    IllegalTransition,
    OperatorDecision,
    StaleRequest,
    State,
)
from cua.escalation.lease import ControlLease, LeasedSurface, LeaseHeld
from cua.escalation.operator_app import create_app
from cua.escalation.requests import Queue, token_sha256
from cua.evidence.logger import RunLog
from cua.replay.engine import ReplayConfig, ReplayEngine, SavedRun
from cua.replay.handoff import (
    Abort,
    Decision,
    HandBack,
    HandoffRequest,
    Ticket,
    Unanswered,
)
from cua.replay.invocation import Budget, Invocation
from cua.replay.result import Escalated, Failure, Success
from cua.replay.resume import find_resume_point
from cua.replay.waits import PausableClock
from cua.secrets.resolver import Credential
from cua.surface.locators import RoleName
from cua.surface.protocol import Click, Observation, SessionHandle, Viewport
from tests.unit import screens
from tests.unit.fakes import FakeSurface
from tests.unit.test_discovery_loop import CREDENTIAL, GOAL, framed
from tests.unit.test_discovery_loop import POLICY as LOOP_POLICY
from tests.unit.test_discovery_loop import REVIEW as LOOP_REVIEW
from tests.unit.test_discovery_loop import TENANT as LOOP_TENANT
from tests.unit.test_replay import (
    BASE,
    CONSENT,
    EV,
    GOAL1,
    POLICY,
    TENANT,
    TickingClock,
    confirm_only,
    goal1,
    opened,
    review,
    signed_in,
)

INPUTS = {"member_id": "10003", "initial_deposit": "250.00"}
STATES: list[State] = ["AUTOMATION", "PAUSED", "HUMAN_IN_CONTROL", "RESUMING", "ABORTED"]


# --------------------------------------------------------------------------
# The state machine (plan row 28)
# --------------------------------------------------------------------------


def test_the_state_machine_is_the_one_drawn() -> None:
    assert TRANSITIONS == {
        "AUTOMATION": {"PAUSED"},
        "PAUSED": {"HUMAN_IN_CONTROL", "ABORTED"},
        "HUMAN_IN_CONTROL": {"RESUMING", "ABORTED"},
        "RESUMING": {"AUTOMATION", "PAUSED"},
        "ABORTED": set(),
    }


ILLEGAL = [(a, b) for a, b in product(STATES, STATES) if b not in TRANSITIONS[a]]


@pytest.mark.parametrize(("start", "to"), ILLEGAL)
def test_every_illegal_transition_raises(tmp_path: Path, start: State, to: State) -> None:
    store = ControlStore(tmp_path)
    store.start("run_x")
    _walk_to(store, start)
    with pytest.raises(IllegalTransition):
        store.transition(to, by="test")
    assert store.read().state == start


def _walk_to(store: ControlStore, state: State) -> None:
    path: dict[State, list[State]] = {
        "AUTOMATION": [],
        "PAUSED": ["PAUSED"],
        "HUMAN_IN_CONTROL": ["PAUSED", "HUMAN_IN_CONTROL"],
        "RESUMING": ["PAUSED", "HUMAN_IN_CONTROL", "RESUMING"],
        "ABORTED": ["PAUSED", "ABORTED"],
    }
    for step in path[state]:
        store.transition(step, by="test", request_id="req_1" if step == "PAUSED" else None)


def test_the_lease_follows_the_state_and_every_change_is_logged(tmp_path: Path) -> None:
    store = ControlStore(tmp_path)
    store.start("run_x")
    assert store.lease().holder == "automation"
    store.transition("PAUSED", by="automation", request_id="req_1")
    assert (store.lease().holder, store.lease().request_id) == ("none", "req_1")
    store.transition("HUMAN_IN_CONTROL", by="ann", expect_request="req_1")
    assert store.lease().holder == "human"
    decision = OperatorDecision(kind="hand_back", by="ann", resume_at="search.submit")
    store.transition("RESUMING", by="ann", expect_request="req_1", decision=decision)
    assert store.lease().holder == "automation"
    assert store.read().decision == decision
    store.transition("AUTOMATION", by="automation")
    # A second request starts with no answer.
    store.transition("PAUSED", by="automation", request_id="req_2")
    assert store.read().decision is None

    log = store.transitions()
    assert [(e["from"], e["to"]) for e in log] == [
        (None, "AUTOMATION"),
        ("AUTOMATION", "PAUSED"),
        ("PAUSED", "HUMAN_IN_CONTROL"),
        ("HUMAN_IN_CONTROL", "RESUMING"),
        ("RESUMING", "AUTOMATION"),
        ("AUTOMATION", "PAUSED"),
    ]
    assert log[2]["by"] == "ann" and log[3]["decision"]["resume_at"] == "search.submit"


def test_an_answer_to_an_older_request_is_refused(tmp_path: Path) -> None:
    store = ControlStore(tmp_path)
    store.start("run_x")
    store.transition("PAUSED", by="automation", request_id="req_2")
    with pytest.raises(StaleRequest):
        store.transition("HUMAN_IN_CONTROL", by="ann", expect_request="req_1")


# --------------------------------------------------------------------------
# The lease (plan row 28)
# --------------------------------------------------------------------------


def test_the_automation_cannot_act_while_a_person_holds_the_lease() -> None:
    holder: list[ControlLease] = [ControlLease(holder="human", since="t", request_id="req_1")]
    inner = FakeSurface([review(), opened()])
    surface = LeasedSurface(inner, lambda: holder[0])
    screen = surface.observe()  # looking is not acting
    button = next(n for n in screen.nodes if n.role == "button")
    with pytest.raises(LeaseHeld, match="held by human"):
        surface.act(Click(ref=button.ref))
    holder[0] = ControlLease(holder="none", since="t")
    with pytest.raises(LeaseHeld):
        surface.act(Click(ref=button.ref))
    assert inner.actions == []
    holder[0] = ControlLease(holder="automation", since="t")
    surface.act(Click(ref=button.ref))
    assert len(inner.actions) == 1


# --------------------------------------------------------------------------
# Timers stop while paused (plan row 28)
# --------------------------------------------------------------------------


class ManualClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t


def test_a_paused_clock_does_not_count_the_pause() -> None:
    base = ManualClock()
    clock = PausableClock(base)
    base.t = 5.0
    clock.pause()
    base.t = 1005.0
    assert clock.now() == 5.0 and clock.paused_s == 1000.0
    clock.resume()
    base.t = 1007.0
    assert clock.now() == 7.0


# --------------------------------------------------------------------------
# The engine's side of a handoff
# --------------------------------------------------------------------------


class FakeHandoff:
    """A channel to a person, in memory: answers in order, and can change the
    screen while "waiting" the way a person in the browser would."""

    def __init__(
        self,
        *decisions: Decision,
        on_wait: Callable[[int], None] | None = None,
        actions: int = 0,
    ) -> None:
        self.decisions = list(decisions)
        self.requests: list[HandoffRequest] = []
        self.resumed_at: list[tuple[str, str | None, str]] = []
        self.on_wait = on_wait
        self.actions = actions

    def open(self, request: HandoffRequest) -> Ticket:
        self.requests.append(request)
        n = len(self.requests)
        return Ticket(
            request_id=f"req_{n}", resume_token=f"rsm_{n}", intervention=f"interventions/{n}.json"
        )

    def wait(self, ticket: Ticket) -> Decision:
        if self.on_wait is not None:
            self.on_wait(len(self.requests))
        return self.decisions.pop(0)

    def resumed(self, ticket: Ticket, *, checkpoint: str | None, next_step: str) -> None:
        self.resumed_at.append((ticket.request_id, checkpoint, next_step))

    def human_actions(self, ticket: Ticket) -> int:
        return self.actions


def build(
    tmp_path: Path,
    surface: FakeSurface,
    handoff: FakeHandoff | None,
    *,
    token: bool = False,
    budget: Budget | None = None,
    capability: Capability | None = None,
    clock: object | None = None,
    inputs: dict[str, str] | None = None,
) -> ReplayEngine:
    return ReplayEngine(
        capability=capability or confirm_only(),
        surface=surface,
        tenant=TENANT,
        policy=POLICY,
        invocation=Invocation(inputs=inputs or INPUTS, budget=budget or Budget()),
        approval=CONSENT if token else None,
        credentials={
            "app_login": Credential(
                "secret://local/x", {"username": "opuser", "password": "hunter2pw"}
            )
        },
        log=RunLog(tmp_path / "run"),
        config=ReplayConfig(screenshots=False),
        clock=clock or TickingClock(),  # type: ignore[arg-type]
        handoff=handoff,
    )


def clicks(surface: FakeSurface) -> int:
    return len([a for a in surface.actions if isinstance(a, Click)])


def events(tmp_path: Path) -> list[dict[str, object]]:
    lines = (tmp_path / "run" / "log.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_an_operator_approval_lets_the_commit_through_once(tmp_path: Path) -> None:
    """Plan row 14, engine half: no token, a person approves, one commit."""
    handoff = FakeHandoff(HandBack(by="ops", approved=True))
    surface = FakeSurface([review(), opened()])
    result = build(tmp_path, surface, handoff).run()

    assert isinstance(result, Success), result
    assert result.side_effect == "committed"
    assert clicks(surface) == 1
    asked = handoff.requests[0]
    assert (asked.reason, asked.code, asked.step_id) == (
        "NEEDS_APPROVAL",
        "POLICY_BLOCKED",
        "review.submit",
    )
    assert "approve" in asked.options and "retry_step" not in asked.options
    assert asked.screenshot is not None  # taken even on a run without screenshots
    (handed,) = result.handoffs
    assert (handed.decision, handed.decided_by, handed.resumed_at) == (
        "approve",
        "ops",
        "review.submit",
    )
    approved = [e for e in events(tmp_path) if e["event"] == "policy.approved"]
    assert approved[0]["approved_by"] == "ops" and approved[0]["via"] == "console"
    assert result.evidence.intervention == "interventions/1.json"


def test_allow_escalation_false_aborts_the_escalation_and_asks_nobody(tmp_path: Path) -> None:
    """Plan row 17, engine half."""
    handoff = FakeHandoff()
    surface = FakeSurface([review(), opened()])
    budget = Budget(allow_escalation=False)
    result = build(tmp_path, surface, handoff, budget=budget).run()

    assert isinstance(result, Failure)
    assert result.code == "ESCALATION_ABORTED"
    assert result.escalation_reason == "NEEDS_APPROVAL"
    assert "POLICY_BLOCKED" in result.message and "allow_escalation is false" in result.message
    assert handoff.requests == []
    assert clicks(surface) == 0


def test_an_operator_abort_ends_the_run_with_nothing_committed(tmp_path: Path) -> None:
    handoff = FakeHandoff(Abort(by="ops", why="wrong member"))
    surface = FakeSurface([review(), opened()])
    result = build(tmp_path, surface, handoff).run()

    assert isinstance(result, Failure)
    assert (result.code, result.side_effect) == ("ESCALATION_ABORTED", "none")
    assert "ops aborted the run at review.submit" in result.message
    assert "wrong member" in result.message
    assert result.handoffs[0].decision == "abort"
    assert clicks(surface) == 0


def test_nobody_answering_returns_escalated_with_a_resume_token(tmp_path: Path) -> None:
    handoff = FakeHandoff(Unanswered(why="nobody took the request within 0s"))
    engine = build(tmp_path, FakeSurface([review(), opened()]), handoff)
    result = engine.run()

    assert isinstance(result, Escalated), result
    assert (result.reason, result.step_id, result.resume_token) == (
        "NEEDS_APPROVAL",
        "review.submit",
        "rsm_1",
    )
    assert result.side_effect == "none"
    assert engine.suspended is not None
    saved = json.loads((tmp_path / "run" / "result.json").read_text())
    assert saved["kind"] == "escalated"


def test_a_suspended_run_is_carried_on_by_another_engine(tmp_path: Path) -> None:
    """What `cua resume` does: a fresh engine, the saved state, the answer."""
    first = build(tmp_path, FakeSurface([review()]), FakeHandoff(Unanswered(why="later")))
    assert isinstance(first.run(), Escalated)
    assert first.suspended is not None
    saved = SavedRun.model_validate_json(first.suspended.model_dump_json())

    surface = FakeSurface([review(), opened()])
    second = build(tmp_path, surface, FakeHandoff(HandBack(by="ops", approved=True)))
    result = second.resume(saved)
    assert isinstance(result, Success), result
    assert result.side_effect == "committed" and clicks(surface) == 1
    assert result.handoffs[-1].decision == "approve"
    assert [e["event"] for e in events(tmp_path)].count("run.resumed") == 1


def test_a_person_who_commits_is_credited_and_nothing_is_done_twice(tmp_path: Path) -> None:
    """The M6 case "the operator completes the rest of the flow": resume
    finds cp.done, the commit is recorded as the person's, no step runs."""
    surface = FakeSurface([review(), opened()])

    def person_confirms(_: int) -> None:
        surface.index = 1  # the person pressed Confirm in the browser

    handoff = FakeHandoff(HandBack(by="ops"), on_wait=person_confirms, actions=2)
    result = build(tmp_path, surface, handoff).run()

    assert isinstance(result, Success), result
    assert result.side_effect == "committed"
    assert result.outputs == {"reference_number": "REF-10003-0001"}
    assert clicks(surface) == 0
    (handed,) = result.handoffs
    assert (handed.resumed_at, handed.resumed_after_checkpoint, handed.human_actions_count) == (
        "done",
        "cp.done",
        2,
    )
    committed = [e for e in events(tmp_path) if e["event"] == "irreversible.committed"]
    assert committed[0]["performed_by"] == "person"
    assert handoff.resumed_at == [("req_1", "cp.done", "done")]


def test_a_commit_that_may_have_happened_is_never_retried_and_stays_unknown(
    tmp_path: Path,
) -> None:
    """The press went out and never landed. Retry is not offered; asked for
    anyway, it is refused; the screen proves nothing, so the person is asked
    again; aborted, the side effect is still unknown — never none."""
    handoff = FakeHandoff(HandBack(by="ops", retry=True), Abort(by="ops"))
    surface = FakeSurface([review()])  # the screen never changes
    result = build(tmp_path, surface, handoff, token=True).run()

    assert isinstance(result, Failure), result
    assert (result.code, result.side_effect) == ("ESCALATION_ABORTED", "unknown")
    assert clicks(surface) == 1
    first, second = handoff.requests
    assert first.code == "TIMEOUT" and first.side_effect == "unknown"
    assert "retry_step" not in first.options
    assert all(c.step_id == "done" for c in first.resume_points)
    assert (second.attempt, second.code) == (2, "CHECKPOINT_FAILED")
    assert "handoff.retry_refused" in [e["event"] for e in events(tmp_path)]


def test_where_the_operator_says_to_resume_is_checked_not_trusted(tmp_path: Path) -> None:
    handoff = FakeHandoff(HandBack(by="ops", resume_at="done"), Abort(by="ops"))
    surface = FakeSurface([review(), opened()])
    result = build(tmp_path, surface, handoff).run()

    assert isinstance(result, Failure) and result.code == "ESCALATION_ABORTED"
    assert handoff.requests[1].code == "CHECKPOINT_FAILED"
    assert clicks(surface) == 0


def test_the_budget_does_not_run_while_a_person_has_the_controls(tmp_path: Path) -> None:
    """Plan row 28: an hour waiting for a person is not an hour of the run's
    two-minute budget."""
    base = ManualClock()

    class Ticking:
        def now(self) -> float:
            base.t += 0.05
            return base.t

    def an_hour_passes(_: int) -> None:
        base.t += 3600.0

    handoff = FakeHandoff(HandBack(by="ops", approved=True), on_wait=an_hour_passes)
    surface = FakeSurface([review(), opened()])
    engine = build(tmp_path, surface, handoff, budget=Budget(timeout_s=120), clock=Ticking())
    result = engine.run()
    assert isinstance(result, Success), result
    assert result.duration_ms < 120_000


def test_done_holds_on_resume_only_with_the_checkpoint_before_it() -> None:
    """A person who wandered to another member's page must not end the run
    with that member's balance."""
    cap = goal1()
    other = signed_in(screens.MEMBER_DETAIL.replace("10003", "10004"), f"{BASE}/member/10004")
    outputs = {"savings_balance": "1.00"}
    point = find_resume_point(cap, other, EV, inputs={"member_id": "10003"}, outputs=outputs)
    assert point is None or point.checkpoint != "cp.done"
    ours = signed_in(screens.MEMBER_DETAIL, f"{BASE}/member/10003")
    point = find_resume_point(cap, ours, EV, inputs={"member_id": "10003"}, outputs=outputs)
    assert point is not None and point.checkpoint == "cp.done"


def test_without_a_channel_the_escalation_is_a_failure_as_before(tmp_path: Path) -> None:
    surface = FakeSurface([review(), opened()])
    result = build(tmp_path, surface, None).run()
    assert isinstance(result, Failure)
    assert (result.code, result.escalation_reason) == ("POLICY_BLOCKED", "NEEDS_APPROVAL")


# --------------------------------------------------------------------------
# Requests, the queue and the channel, on files
# --------------------------------------------------------------------------

SESSION = SessionHandle(
    cdp_url="http://127.0.0.1:9",
    page_url=f"{BASE}/",
    viewport=Viewport(w=1280, h=800),
    target_id="T1",
)


def channel(tmp_path: Path, **settings: object) -> OperatorChannel:
    runs = tmp_path / "runs"
    log = RunLog(runs / "run_1")
    control = ControlStore(log.dir)
    control.start(log.run_id)
    return OperatorChannel(
        log=log,
        runs_dir=runs,
        control=control,
        session=lambda: SESSION,
        kind="replay",
        capability="open_subaccount",
        capability_version=2,
        tenant="local",
        settings=HandoffSettings.model_validate({"poll_s": 0.01, **settings}),
    )


REQUEST = HandoffRequest(
    step_id="review.submit",
    reason="NEEDS_APPROVAL",
    code="POLICY_BLOCKED",
    message="review.submit commits a change and needs approval",
    observed="[main] ...",
    screenshot="screenshots/0001.png",
    options=["take_control", "resume", "approve", "abort"],
)


def test_a_request_carries_what_a_person_needs_and_the_token_stays_out_of_files(
    tmp_path: Path,
) -> None:
    ch = channel(tmp_path)
    ticket = ch.open(REQUEST)

    written = json.loads((ch.log.dir / ticket.intervention).read_text())
    assert written["capability"] == "open_subaccount" and written["step_id"] == "review.submit"
    assert written["reason_code"] == "NEEDS_APPROVAL"
    assert written["masked_screenshot"] == "screenshots/0001.png"
    assert written["cdp_url"] == SESSION.cdp_url and written["target_id"] == "T1"
    assert written["devtools_url"].endswith("/devtools/page/T1")
    assert ch.control.read().state == "PAUSED"

    queue = Queue(tmp_path / "runs")
    entry = queue.by_token(ticket.resume_token)
    assert entry is not None and entry.request_id == ticket.request_id
    assert entry.token_sha256 == token_sha256(ticket.resume_token)
    for path in (tmp_path / "runs").rglob("*"):
        if path.is_file():
            assert ticket.resume_token not in path.read_text(encoding="utf-8"), path


def test_the_channel_hands_back_what_the_console_decided(tmp_path: Path) -> None:
    ch = channel(tmp_path)
    ticket = ch.open(REQUEST)
    ch.control.transition("HUMAN_IN_CONTROL", by="ann", expect_request=ticket.request_id)
    decision = OperatorDecision(kind="hand_back", by="ann", resume_at="done")
    ch.control.transition("RESUMING", by="ann", expect_request=ticket.request_id, decision=decision)
    assert ch.wait(ticket) == HandBack(by="ann", resume_at="done")
    ch.resumed(ticket, checkpoint="cp.done", next_step="done")
    assert ch.control.read().state == "AUTOMATION"


def test_nobody_taking_the_request_in_time_is_unanswered(tmp_path: Path) -> None:
    ch = channel(tmp_path, wait_s=0)
    ticket = ch.open(REQUEST)
    assert isinstance(ch.wait(ticket), Unanswered)
    assert ch.control.read().state == "PAUSED"  # still open, for the console


def test_an_expired_request_is_aborted_by_timeout(tmp_path: Path) -> None:
    ch = channel(tmp_path, ttl_s=0.001)
    ticket = ch.open(REQUEST)
    decision = ch.wait(ticket)
    assert isinstance(decision, Abort) and decision.by == "timeout"
    assert ch.control.read().state == "ABORTED"


def test_only_browser_actions_under_the_request_are_counted(tmp_path: Path) -> None:
    lines = [
        {"request_id": "req_1", "source": "console", "action": "take_control"},
        {"request_id": "req_1", "source": "browser", "action": "click"},
        {"request_id": "req_1", "source": "browser", "action": "navigate"},
        {"request_id": "req_2", "source": "browser", "action": "click"},
    ]
    (tmp_path / HUMAN_ACTIONS_FILE).write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    assert count_human_actions(tmp_path, "req_1") == 2


# --------------------------------------------------------------------------
# The operator console
# --------------------------------------------------------------------------


def test_the_console_lists_requests_and_records_every_decision_by_name(tmp_path: Path) -> None:
    ch = channel(tmp_path)
    ticket = ch.open(REQUEST)
    client = TestClient(create_app(tmp_path / "runs"))

    listed = client.get("/api/requests").json()
    assert [v["request"]["id"] for v in listed] == [ticket.request_id]
    assert listed[0]["state"] == "PAUSED"
    assert ticket.request_id in client.get("/").text
    assert "Approve action" in client.get(f"/requests/{ticket.request_id}").text

    nameless = client.post(f"/api/requests/{ticket.request_id}/approve", json={"by": " "})
    assert nameless.status_code == 400
    unoffered = client.post(f"/api/requests/{ticket.request_id}/retry_step", json={"by": "ann"})
    assert unoffered.status_code == 409

    approved = client.post(f"/api/requests/{ticket.request_id}/approve", json={"by": "ann"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "RESUMING"
    assert ch.wait(ticket) == HandBack(by="ann", approved=True)
    # Deciding without taking control still passes through the person's hands.
    assert [(e["from"], e["to"]) for e in ch.control.transitions()][-2:] == [
        ("PAUSED", "HUMAN_IN_CONTROL"),
        ("HUMAN_IN_CONTROL", "RESUMING"),
    ]
    recorded = (ch.log.dir / HUMAN_ACTIONS_FILE).read_text()
    assert '"source": "console"' in recorded and '"action": "approve"' in recorded

    again = client.post(f"/api/requests/{ticket.request_id}/abort", json={"by": "ann"})
    assert again.status_code == 409  # handed back already


# --------------------------------------------------------------------------
# Discovery with a person on the channel
# --------------------------------------------------------------------------


def discover(
    tmp_path: Path, steps: list[ScriptStep], surface: FakeSurface, handoff: FakeHandoff
) -> tuple[object, RunLog]:
    log = RunLog(tmp_path / "run")
    outcome = DiscoveryLoop(
        surface=surface,
        llm=ScriptedClient(Script(steps=steps)),
        goal=GOAL,
        tenant=LOOP_TENANT,
        policy=LOOP_POLICY,
        credentials={"app_login": CREDENTIAL},
        log=log,
        config=DiscoveryConfig(),
        handoff=handoff,
    ).run()
    return outcome, log


CONFIRM = [ScriptStep(tool="click", target=[RoleName(role="button", name="Confirm")])]


def test_discovery_asks_the_console_before_a_risky_action_and_acts_on_approval(
    tmp_path: Path,
) -> None:
    surface = FakeSurface([framed(LOOP_REVIEW, "/review/10003")])

    def refs_renumbered(_: int) -> None:
        # Every look mints new refs; the one the agent chose is spent by now.
        text = surface.screens[0].model_dump_json()
        surface.screens[0] = Observation.model_validate_json(re.sub(r'"n(\d+)"', r'"n9\1"', text))

    # The script then runs out, and the agent says stuck: that is asked too.
    handoff = FakeHandoff(
        HandBack(by="ann", approved=True), Abort(by="ann"), on_wait=refs_renumbered
    )
    outcome, log = discover(tmp_path, CONFIRM, surface, handoff)

    assert handoff.requests[0].reason == "NEEDS_APPROVAL"
    # The Confirm, once approved, found again under its new ref.
    assert len(surface.acted) == 1 and surface.acted[0].startswith("click:n9")
    lines = [json.loads(x) for x in (log.dir / "log.jsonl").read_text().splitlines()]
    approved = [e for e in lines if e["event"] == "policy.approved"]
    assert approved[0]["via"] == "console" and approved[0]["approved_by"] == "ann"
    assert outcome.handoffs[0].decision == "approve"  # type: ignore[attr-defined]
    assert outcome.human_assisted is False  # type: ignore[attr-defined]


def test_discovery_handed_back_without_approval_does_not_act(tmp_path: Path) -> None:
    surface = FakeSurface([framed(LOOP_REVIEW, "/review/10003")])
    handoff = FakeHandoff(HandBack(by="ann"), Abort(by="ann"), actions=1)
    outcome, _ = discover(tmp_path, CONFIRM, surface, handoff)
    assert surface.acted == []
    assert outcome.human_assisted is True  # type: ignore[attr-defined]


def test_discovery_aborted_on_the_console_stops(tmp_path: Path) -> None:
    surface = FakeSurface([framed(LOOP_REVIEW, "/review/10003")])
    outcome, _ = discover(tmp_path, CONFIRM, surface, FakeHandoff(Abort(by="ann")))
    assert (outcome.kind, outcome.reason) == ("stopped", "ESCALATION_ABORTED")  # type: ignore[attr-defined]
    assert surface.acted == []


def test_a_run_a_person_helped_with_is_not_recorded(tmp_path: Path) -> None:
    import shutil

    source = Path(__file__).resolve().parents[1] / "fixtures" / "runs" / "goal1-scripted"
    run_dir = tmp_path / "goal1"
    shutil.copytree(source, run_dir)
    result = json.loads((run_dir / "result.json").read_text())
    (run_dir / "result.json").write_text(json.dumps({**result, "human_assisted": True}))
    with pytest.raises(RecordError, match="with a person's help"):
        record(run_dir, policy=POLICY, families_dir=GOAL1.parent / "families")


def test_a_heartbeat_the_console_is_reading_never_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows will not delete a file another process has open; the run must
    stop waiting all the same (the stale heartbeat reads as nobody waiting)."""
    ch = channel(tmp_path, wait_s=0)
    ticket = ch.open(REQUEST)

    def in_use(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(32, "being used by another process")

    monkeypatch.setattr(Path, "unlink", in_use)
    assert isinstance(ch.wait(ticket), Unanswered)
