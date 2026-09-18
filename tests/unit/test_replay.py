"""Replay, without a browser: the rules the engine applies, and the engine
itself over a fake surface.

What is tested here is what must hold whatever the target app does — scope
and precedence of detectors, the resume-state search, input validation,
parsing, the recovery caps, the idempotency cache, and above all that an
irreversible step is performed once and never again. The integration tests
replay the same capabilities against the real mock app.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cua.artifact.schema import Capability
from cua.artifact.store import load
from cua.evidence.logger import RunLog
from cua.policy.allowlist import load_policy
from cua.policy.tokens import Approval
from cua.replay import detectors
from cua.replay.engine import ReplayConfig, ReplayEngine
from cua.replay.extract import ParseError, parse
from cua.replay.invocation import (
    ApprovalGrant,
    Budget,
    Invocation,
    bind,
    request_summary,
    validate_inputs,
)
from cua.replay.recoverers import RecoveryLedger
from cua.replay.result import (
    RESULT,
    BusinessOutcome,
    Escalated,
    Failure,
    IdempotencyCache,
    IdempotencyConflict,
    Success,
    exit_code,
    fingerprint,
    to_json,
)
from cua.replay.resume import find_resume_point
from cua.secrets.resolver import Credential
from cua.surface.conditions import LocationMatches, TextPresent
from cua.surface.evaluators import WebEvaluator
from cua.surface.protocol import Click, Observation
from cua.tenant import Tenant
from tests.unit import screens
from tests.unit.artifacts import GOAL1_RUN, record_goal1
from tests.unit.fakes import FakeSurface

REPO = Path(__file__).resolve().parents[2]
GOAL1 = REPO / "capabilities" / "member_savings_balance.json"
GOAL2 = REPO / "capabilities" / "open_subaccount.json"
GOAL2_RUN = REPO / "tests" / "fixtures" / "runs" / "goal2-scripted"
BASE = "http://127.0.0.1:8000"
SHELL = f"{BASE}/"
TENANT = Tenant(id="local", app_family="legacy-core", base_url=BASE)
POLICY = load_policy(REPO / "policies" / "default.yaml", TENANT)
EV = WebEvaluator()


def goal1() -> Capability:
    return load(GOAL1)


def signed_in(main: str, main_url: str) -> Observation:
    return screens.build(
        {"nav": screens.NAV, "main": main},
        location=SHELL,
        urls={"": SHELL, "nav": f"{BASE}/nav", "main": main_url},
        frame_x={"nav": 10.0, "main": 200.0},
    )


# --------------------------------------------------------------------------
# Purity (plan row 24)
# --------------------------------------------------------------------------


def test_replay_imports_no_model_client_and_no_agent() -> None:
    """Determinism is a property of what replay can reach, so prove it in a
    clean interpreter: import everything replay is made of, then look."""
    code = (
        "import sys\n"
        "import cua.replay.runner, cua.replay.engine, cua.replay.result\n"
        "import cua.replay.detectors, cua.replay.recoverers, cua.replay.resume\n"
        "bad = [m for m in sys.modules if m == 'anthropic' or m.startswith(('anthropic.',"
        " 'google.genai', 'cua.agent'))]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=REPO
    )
    assert out.stdout.strip() == ""


# --------------------------------------------------------------------------
# Detector scope and precedence (plan row 12)
# --------------------------------------------------------------------------


def codes(cap: Capability, step: str) -> list[str]:
    index = [s.id for s in cap.steps].index(step)
    return [d.code for d in detectors.listening(cap, index)]


def test_session_expiry_is_deaf_while_the_run_is_still_signing_on() -> None:
    cap = goal1()
    # The detector's own condition is true on the sign-on screen...
    login = screens.build({"": screens.LOGIN}, location=f"{BASE}/login", urls={"": f"{BASE}/login"})
    expired = next(d for d in cap.outcome_detectors if d.code == "SESSION_EXPIRED")
    assert EV.evaluate(expired.match, login)
    # ...so only its scope keeps it quiet there.
    assert "SESSION_EXPIRED" not in codes(cap, "login.username")
    assert "SESSION_EXPIRED" not in codes(cap, "login.password")
    assert "SESSION_EXPIRED" in codes(cap, "login.submit")
    assert "SESSION_EXPIRED" in codes(cap, "search.submit")


def test_after_step_detectors_listen_only_after_that_step() -> None:
    cap = goal1()
    assert "NOT_FOUND" in codes(cap, "search.submit")
    assert "NOT_FOUND" not in codes(cap, "search.member_id")
    assert "APP_ERROR" in codes(cap, "login.username")  # unscoped: everywhere


def test_hard_beats_business_beats_recoverable() -> None:
    cap = goal1()
    listening = detectors.listening(cap, [s.id for s in cap.steps].index("search.submit"))
    order = [d.class_ for d in listening]
    assert order == sorted(order, key=detectors.PRECEDENCE.__getitem__)
    assert order[0] == "hard"


def test_timeout_detectors_are_not_screen_detectors() -> None:
    cap = goal1()
    assert all(d.code != "SLOW_LOAD" for d in detectors.listening(cap, 4))
    found = detectors.on_timeout(cap)
    assert found is not None and found.code == "SLOW_LOAD"


def test_a_500_at_the_expected_url_is_still_a_500() -> None:
    cap = goal1()
    screen = signed_in(screens.SERVER_ERROR, f"{BASE}/member/10003")
    screen = screen.model_copy(
        update={
            "frames": [
                f.model_copy(update={"status": 500}) if f.name == "main" else f
                for f in screen.frames
            ]
        }
    )
    listening = detectors.listening(cap, 4)
    hit = detectors.first_match(listening, screen, EV)
    assert hit is not None and hit.code == "APP_ERROR"


# --------------------------------------------------------------------------
# Resume-state search (plan row 29)
# --------------------------------------------------------------------------


def test_resume_search_returns_the_newest_checkpoint_that_holds() -> None:
    cap = goal1()
    detail = signed_in(screens.MEMBER_DETAIL, f"{BASE}/member/10003")
    point = find_resume_point(cap, detail, EV, inputs={"member_id": "10003"})
    assert point is not None
    assert point.checkpoint == "cp.member_detail"
    assert cap.steps[point.next_step - 1].id == "search.submit"


def test_resume_search_falls_back_to_an_older_checkpoint() -> None:
    cap = goal1()
    search = signed_in(screens.SEARCH, f"{BASE}/search")
    point = find_resume_point(cap, search, EV, inputs={"member_id": "10003"})
    assert point is not None and point.checkpoint == "cp.logged_in"
    assert cap.steps[point.next_step].id == "search.member_id"


def test_resume_search_checks_the_bound_input_not_just_the_screen() -> None:
    cap = goal1()
    detail = signed_in(screens.MEMBER_DETAIL, f"{BASE}/member/10003")
    point = find_resume_point(cap, detail, EV, inputs={"member_id": "10004"})
    # Somebody else's detail page is not this run's checkpoint, and it is not
    # the search screen either: nothing holds.
    assert point is None


def test_resume_search_reaches_done_only_once_outputs_are_read() -> None:
    cap = goal1()
    detail = signed_in(screens.MEMBER_DETAIL, f"{BASE}/member/10003")
    point = find_resume_point(
        cap,
        detail,
        EV,
        inputs={"member_id": "10003"},
        outputs={"savings_balance": "1411.21"},
    )
    assert point is not None and point.checkpoint == "cp.done"
    assert point.next_step == len(cap.steps)


def test_resume_search_never_goes_back_before_a_committed_step() -> None:
    cap = goal1()
    search = signed_in(screens.SEARCH, f"{BASE}/search")
    point = find_resume_point(cap, search, EV, inputs={"member_id": "10003"}, not_before=5)
    assert point is None


# --------------------------------------------------------------------------
# Invocation (plan row 20)
# --------------------------------------------------------------------------


def test_inputs_are_checked_against_the_declaration() -> None:
    cap = load(GOAL2)
    assert validate_inputs(cap, {"member_id": "10003", "initial_deposit": "250.00"}) == []
    problems = validate_inputs(cap, {"member_id": "10O03", "colour": "red"})
    text = "; ".join(problems)
    assert "'colour' is not an input" in text
    assert "'initial_deposit' is required" in text
    assert "member_id='10O03' does not match" in text
    assert validate_inputs(cap, {"member_id": "1", "initial_deposit": "lots"}) == [
        "initial_deposit='lots' is not a decimal"
    ]


def test_bound_patterns_escape_the_value() -> None:
    bound = bind(LocationMatches(pattern="/member/${member_id}$"), {"member_id": "1.3"})
    assert bound.pattern == r"/member/1\.3$"
    text = bind(TextPresent(text="id ${member_id}"), {"member_id": "1.3"})
    assert text.text == "id 1.3"


def test_the_logged_request_never_carries_the_token() -> None:
    cap = goal1()
    invocation = Invocation(
        inputs={"member_id": "10003"},
        approval=ApprovalGrant(token="s3cret-token", approved_by="ops"),
    )
    logged = json.dumps(request_summary(invocation, cap))
    assert "s3cret-token" not in logged
    assert '"approved_by": "ops"' in logged


def test_budget_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        Budget(timeout_s=0)
    with pytest.raises(ValueError):
        Budget.model_validate({"max_recoveries": 1, "retries": 4})


# --------------------------------------------------------------------------
# Parsing outputs
# --------------------------------------------------------------------------


def test_outputs_parse_strictly() -> None:
    cap = goal1()
    balance = cap.outputs["savings_balance"]
    assert parse("$1,411.21", balance) == "1411.21"
    assert parse("($12.50)", balance) == "-12.50"
    assert parse("-$3.00", balance) == "-3.00"
    for bad in ("", "n/a", "$1.2.3", "12 dollars"):
        with pytest.raises(ParseError):
            parse(bad, balance)
    assert parse("  Test Member 03 ", cap.outputs["member_name"]) == "Test Member 03"


# --------------------------------------------------------------------------
# Recovery caps
# --------------------------------------------------------------------------


def test_recovery_caps_apply_per_step_per_detector_and_per_run() -> None:
    cap = goal1()
    step = next(s for s in cap.steps if s.id == "search.submit")
    notice = next(d for d in cap.outcome_detectors if d.code == "INTERSTITIAL")
    slow = next(d for d in cap.outcome_detectors if d.code == "SLOW_LOAD")

    ledger = RecoveryLedger(cap.recovery_limits, max_recoveries=3)
    assert ledger.refusal(step, notice) is None
    ledger.record(step, notice, "dismiss_notice")
    assert "recoveries a step is allowed" in (ledger.refusal(step, slow) or "")

    tight = RecoveryLedger(cap.recovery_limits, max_recoveries=0)
    assert "the run has used all 0" in (tight.refusal(step, notice) or "")


def test_an_irreversible_step_is_never_retried() -> None:
    cap = load(GOAL2)
    confirm = next(s for s in cap.steps if s.risk == "irreversible")
    slow = next(d for d in cap.outcome_detectors if d.code == "SLOW_LOAD")
    refusal = RecoveryLedger(cap.recovery_limits, 3).refusal(confirm, slow)
    assert refusal is not None and "irreversible" in refusal


# --------------------------------------------------------------------------
# Result contract and idempotency (plan row 19)
# --------------------------------------------------------------------------


def test_each_kind_is_its_own_type_with_its_own_exit_code() -> None:
    common = {"capability": "c", "capability_version": 1}
    results = [
        Success(outputs={"x": "1"}, **common),
        BusinessOutcome(code="NOT_FOUND", **common),
        Failure(code="TIMEOUT", side_effect="unknown", **common),
        Escalated(reason="STUCK", request_id="r", resume_token="t", **common),
    ]
    assert [exit_code(r) for r in results] == [0, 2, 1, 3]
    for r in results:
        again = RESULT.validate_json(to_json(r))
        assert type(again) is type(r)
        assert next(iter(json.loads(to_json(r)))) == "kind"


def test_failure_codes_are_a_closed_set() -> None:
    with pytest.raises(ValueError):
        Failure(code="OOPS", capability="c", capability_version=1)


def test_same_key_same_request_returns_the_stored_result(tmp_path: Path) -> None:
    cache = IdempotencyCache(tmp_path)
    request = fingerprint("c", 1, {"member_id": "10003"})
    assert cache.get("k1", request) is None
    stored = Success(outputs={"x": "1"}, capability="c", capability_version=1)
    cache.put("k1", request, stored)
    again = cache.get("k1", request)
    assert isinstance(again, Success) and again.cached and again.outputs == {"x": "1"}
    with pytest.raises(IdempotencyConflict):
        cache.get("k1", fingerprint("c", 1, {"member_id": "10004"}))


def test_the_cache_holds_no_input_values(tmp_path: Path) -> None:
    cache = IdempotencyCache(tmp_path)
    request = fingerprint("c", 1, {"ssn": "123-45-6789"})
    cache.put("k", request, Failure(code="TIMEOUT", capability="c", capability_version=1))
    assert all("123-45-6789" not in p.read_text() for p in cache.dir.iterdir())


# --------------------------------------------------------------------------
# The recorder, for the goal-2 run
# --------------------------------------------------------------------------


def test_the_committed_goal2_capability_is_what_the_recorder_makes_of_its_run() -> None:
    from cua.artifact.recorder import record

    committed = load(GOAL2)
    recorded = record(
        GOAL2_RUN,
        policy=POLICY,
        families_dir=REPO / "capabilities" / "families",
        capability_id=committed.id,
    )
    assert recorded.content_hash() == committed.content_hash()


def test_a_commit_is_irreversible_needs_approval_and_leaves_a_marker() -> None:
    cap = load(GOAL2)
    confirm = next(s for s in cap.steps if s.risk == "irreversible")
    assert confirm.approval == "required" and not confirm.retry.allowed
    assert confirm.side_effect_marker is not None
    # Confirm posts back to its own URL, so the location alone would already
    # be true before the click: the expectation needs the new screen's title.
    assert confirm.expect_after is not None and confirm.expect_after.kind == "all_of"
    assert cap.contract.may_escalate and not cap.contract.idempotent


def test_goal1_recording_is_unchanged_by_the_goal2_rules() -> None:
    assert record_goal1(GOAL1_RUN).steps == goal1().steps


# --------------------------------------------------------------------------
# The engine over a fake surface
# --------------------------------------------------------------------------

REVIEW = """
- heading "Review Sub-account" [level=3]
- table:
  - rowgroup:
    - row "Confirm":
      - cell "Confirm":
        - button "Confirm"
"""

OPENED = """
- heading "Sub-account Opened" [level=3]
- table:
  - rowgroup:
    - row "Field Value":
      - columnheader "Field"
      - columnheader "Value"
    - row "Reference number REF-10003-0001":
      - cell "Reference number"
      - cell "REF-10003-0001"
    - row "Member ID 10003":
      - cell "Member ID"
      - cell "10003"
    - row "Initial Deposit 250.00":
      - cell "Initial Deposit"
      - cell "250.00"
"""


class TickingClock:
    """Every look at the clock moves it on, so deadlines pass without waiting."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        self.t += 0.05
        return self.t


def confirm_only() -> Capability:
    """The last step of open_subaccount, on its own, starting at the review screen."""
    data = load(GOAL2).model_dump(mode="json", by_alias=True)
    confirm = next(s for s in data["steps"] if s["risk"] == "irreversible")
    data["steps"] = [confirm]
    data["target"]["entry"]["pattern"] = "{tenant.base_url}/review/10003"
    data["checkpoints"] = [
        c for c in data["checkpoints"] if c["after_step"] in (confirm["id"], "done")
    ]
    data["outcome_detectors"] = [
        d
        for d in data["outcome_detectors"]
        if not (d.get("scope") or {}).get("after_step")
        and not (d.get("scope") or {}).get("after_checkpoint")
    ]
    data["recoverers"] = {}
    return Capability.model_validate(data)


CONSENT = Approval(
    capability="open_subaccount",
    version=2,
    tenant="local",
    approved_by="ops",
    expires_at=2_000_000_000,
    token_sha256="0" * 64,
)
"""Consent as the runner hands it over once a token has verified."""


def engine(
    tmp_path: Path,
    screens_: list,
    *,
    token: bool = True,
    surface: FakeSurface | None = None,
    capability: Capability | None = None,
) -> tuple[ReplayEngine, FakeSurface]:
    surface = surface or FakeSurface(screens_)
    run = ReplayEngine(
        capability=capability or confirm_only(),
        surface=surface,
        tenant=TENANT,
        policy=POLICY,
        invocation=Invocation(inputs={"member_id": "10003", "initial_deposit": "250.00"}),
        approval=CONSENT if token else None,
        credentials={
            "app_login": Credential(
                "secret://local/x", {"username": "opuser", "password": "hunter2pw"}
            )
        },
        log=RunLog(tmp_path / "run"),
        config=ReplayConfig(screenshots=False),
        clock=TickingClock(),
    )
    return run, surface


def review() -> Observation:
    return signed_in(REVIEW, f"{BASE}/review/10003")


def opened() -> Observation:
    return signed_in(OPENED, f"{BASE}/review/10003")


def test_a_commit_that_lands_is_committed(tmp_path: Path) -> None:
    run, _ = engine(tmp_path, [review(), opened()])
    result = run.run()
    assert isinstance(result, Success), result
    assert result.side_effect == "committed"
    assert result.outputs == {"reference_number": "REF-10003-0001"}


def test_a_commit_that_never_lands_is_unknown_and_is_not_pressed_again(tmp_path: Path) -> None:
    run, surface = engine(tmp_path, [review()])  # the screen never changes
    result = run.run()
    assert isinstance(result, Failure)
    assert result.code == "TIMEOUT"
    assert result.side_effect == "unknown"
    assert "irreversible" in result.message
    assert len([a for a in surface.actions if isinstance(a, Click)]) == 1


def test_a_commit_without_approval_is_not_pressed_at_all(tmp_path: Path) -> None:
    run, surface = engine(tmp_path, [review(), opened()], token=False)
    result = run.run()
    assert isinstance(result, Failure)
    assert result.code == "POLICY_BLOCKED"
    assert result.escalation_reason == "NEEDS_APPROVAL"
    assert result.side_effect == "none"
    assert not [a for a in surface.actions if isinstance(a, Click)]


def test_the_run_directory_records_the_ending(tmp_path: Path) -> None:
    run, _ = engine(tmp_path, [review()])
    run.run()
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "run" / "log.jsonl").read_text().splitlines()
    ]
    assert events[0] == "run.start" and events[-1] == "run.end"
    assert "irreversible.act" in events and "irreversible.committed" not in events
    saved = json.loads((tmp_path / "run" / "result.json").read_text())
    assert saved["kind"] == "failure" and saved["side_effect"] == "unknown"


# --------------------------------------------------------------------------
# States the plan did not enumerate
# --------------------------------------------------------------------------


def log_events(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "run" / "log.jsonl").read_text().splitlines()]


def test_a_refused_sign_on_has_its_own_code_and_listens_only_after_sign_on() -> None:
    cap = goal1()
    auth = next(d for d in cap.outcome_detectors if d.code == "AUTH_FAILED")
    assert auth.class_ == "hard"
    assert "AUTH_FAILED" not in codes(cap, "login.password")
    assert "AUTH_FAILED" in codes(cap, "login.submit")
    # It is not the application failing, so it is not APP_ERROR.
    assert "AUTH_FAILED" in Failure.model_fields["code"].annotation.__args__


class Interrupting(FakeSurface):
    """Ctrl+C lands while the commit is being clicked."""

    def act(self, action):
        if isinstance(action, Click):
            raise KeyboardInterrupt
        return super().act(action)


def test_an_interrupted_run_still_writes_what_it_may_have_committed(tmp_path: Path) -> None:
    run, _ = engine(tmp_path, [review()], surface=Interrupting([review()]))
    with pytest.raises(KeyboardInterrupt):
        run.run()
    saved = json.loads((tmp_path / "run" / "result.json").read_text())
    assert saved["kind"] == "failure" and saved["code"] == "INTERRUPTED"
    assert saved["step_id"] == "review.submit"
    assert saved["side_effect"] == "unknown", "the click was in flight"
    events = [e["event"] for e in log_events(tmp_path)]
    assert "run.interrupted" in events and events[-1] == "run.end"


class LosingTheScreen(FakeSurface):
    """The confirmation screen shows for a few looks, then the session expires
    and the sign-on screen replaces it — before the done step has read it."""

    def __init__(self, screens_, *, keep_for: int) -> None:
        super().__init__(screens_)
        self.keep_for = keep_for
        self.looks = 0

    def observe(self, *, screenshot: bool = False, masks=()) -> Observation:
        if self.index == len(self.screens) - 2:
            self.looks += 1
            if self.looks > self.keep_for:
                self.index = len(self.screens) - 1
        return super().observe(screenshot=screenshot, masks=masks)


def test_outputs_are_read_the_moment_the_commit_lands(tmp_path: Path) -> None:
    expired = signed_in(screens.LOGIN, f"{BASE}/login")
    # One look sees the commit land; the done step's look finds the session gone.
    surface = LosingTheScreen([review(), opened(), expired], keep_for=1)
    run, _ = engine(tmp_path, [], surface=surface)
    result = run.run()
    assert isinstance(result, Failure), result
    assert result.code == "EXTRACTION_FAILED"
    # The commit was seen to land, so it is committed whatever the screen
    # says now — and the caller gets the reference number that was on it.
    assert result.side_effect == "committed"
    assert result.outputs == {"reference_number": "REF-10003-0001"}
    assert any(e["event"] == "outputs.captured_at_commit" for e in log_events(tmp_path))


NOT_FOUND_LATER = """
- heading "Member Search" [level=3]
- paragraph: No matching member
"""


def with_detector(cap: Capability, detector: dict, **extra) -> Capability:
    data = cap.model_dump(mode="json", by_alias=True)
    data["outcome_detectors"].append(detector)
    data.update(extra)
    return Capability.model_validate(data)


def test_a_business_outcome_at_an_unscoped_step_is_still_the_answer(tmp_path: Path) -> None:
    """The member was deleted between the search and the commit: the app says
    "not found" one screen later than the detector was scoped to."""
    cap = with_detector(
        confirm_only(),
        {
            "code": "NOT_FOUND",
            "class": "business",
            "scope": {"after_checkpoint": "cp.done"},  # never in scope during a step
            "match": {"kind": "text_present", "text": "No matching member"},
        },
    )
    gone = signed_in(NOT_FOUND_LATER, f"{BASE}/search")
    run, _ = engine(tmp_path, [review(), gone], capability=cap)
    result = run.run()
    assert isinstance(result, BusinessOutcome), result
    assert result.code == "NOT_FOUND" and result.step_id == "review.submit"
    assert any("outside its declared scope" in w for w in result.warnings), result.warnings
    # The app answered, but the confirm was pressed and nothing proves what it did.
    assert result.side_effect == "unknown"


NOTICE_WITHOUT_OK = """
- heading "System notice" [level=3]
- paragraph: Batch posting is running. Try again later.
"""


def test_a_recovery_that_fails_is_still_in_the_result_and_named_by_the_failure(
    tmp_path: Path,
) -> None:
    cap = with_detector(
        confirm_only(),
        {
            "code": "INTERSTITIAL",
            "class": "recoverable",
            "match": {"kind": "region_present", "name": "System notice"},
            "recover": {"run": "dismiss_notice", "then": "continue"},
        },
        recoverers={
            "dismiss_notice": {
                "steps": [
                    {
                        "action": "click",
                        "target": [{"strategy": "role_name", "role": "button", "name": "OK"}],
                    }
                ]
            }
        },
    )
    notice = signed_in(NOTICE_WITHOUT_OK, f"{BASE}/notice")
    run, _ = engine(tmp_path, [review(), notice], capability=cap)
    result = run.run()
    assert isinstance(result, Failure), result
    assert result.code == "RECOVERY_EXHAUSTED"
    assert result.during_recovery == "INTERSTITIAL at review.submit"
    assert result.message.startswith("during recovery (INTERSTITIAL at review.submit):")
    (attempt,) = result.recoveries
    assert (attempt.code, attempt.action, attempt.outcome) == (
        "INTERSTITIAL",
        "dismiss_notice",
        "failed",
    )
    # The confirm was pressed before the notice appeared; nothing says what it did.
    assert result.side_effect == "unknown"


def test_the_ledger_lists_a_recovery_as_failed_until_it_is_finished() -> None:
    cap = goal1()
    step = next(s for s in cap.steps if s.id == "search.submit")
    notice = next(d for d in cap.outcome_detectors if d.code == "INTERSTITIAL")
    ledger = RecoveryLedger(cap.recovery_limits, max_recoveries=3)
    attempt = ledger.start(step, notice, "dismiss_notice")
    assert [r.outcome for r in ledger.made] == ["failed"]
    ledger.finish(attempt)
    assert [r.outcome for r in ledger.made] == ["succeeded"]
    assert ledger.refusal(step, notice) is not None, "the attempt counted whether or not it worked"


def test_an_escalated_result_is_never_served_from_the_cache(tmp_path: Path) -> None:
    cache = IdempotencyCache(tmp_path)
    request = fingerprint("c", 1, {"member_id": "10003"})
    cache.put(
        "k",
        request,
        Escalated(
            reason="STUCK", request_id="r", resume_token="t", capability="c", capability_version=1
        ),
    )
    assert cache.get("k", request) is None
