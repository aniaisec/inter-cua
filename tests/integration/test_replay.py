"""Replay against the real mock app: one test per row of the plan's replay matrix.

Every test replays a committed capability through the same entry point the CLI
uses (``cua.replay.runner.replay``), in a fresh browser context, with the mock
app's failure injection armed on the first request. The assertions are on the
typed result a calling agent would get — and, where it matters, on what the
mock app says actually happened to it.
"""

from __future__ import annotations

import json
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page

from cua.artifact.store import load, save, with_changes
from cua.policy import tokens
from cua.policy.allowlist import load_policy
from cua.registry.store import Registry
from cua.replay.engine import ReplayConfig
from cua.replay.invocation import ApprovalGrant, Budget, Invocation
from cua.replay.result import BusinessOutcome, Failure, ReplayResult, Success
from cua.replay.runner import replay
from cua.surface.playwright_surface import PlaywrightSurface
from cua.tenant import SecretBinding, Tenant
from mockapp.data import MEMBERS
from mockapp.injects import SLOW_CONFIRM_SECONDS

pytestmark = pytest.mark.browser

REPO = Path(__file__).resolve().parents[2]
GOAL1 = REPO / "capabilities" / "member_savings_balance.json"
GOAL2 = REPO / "capabilities" / "open_subaccount.json"
PASSWORD = "operator"
SIGNING_KEY = "integration-signing-key-0123456789abcdef"
ENV = {"CUA_TEST_OPERATOR": f"operator:{PASSWORD}", "CUA_TEST_SIGNING_KEY": SIGNING_KEY}


class Harness:
    """One replay at a time against the mock app, in the test's own page."""

    def __init__(self, page: Page, base_url: str, tmp_path: Path) -> None:
        self.page = page
        self.base_url = base_url
        self.runs = tmp_path / "runs"
        self.tmp = tmp_path
        self.tenant = Tenant(
            id="local",
            app_family="legacy-core",
            base_url=base_url,
            secrets={
                "mockcore/operator": SecretBinding(
                    var="CUA_TEST_OPERATOR", format="username:password"
                ),
                "cua/approval-signing-key": SecretBinding(var="CUA_TEST_SIGNING_KEY"),
            },
        )
        self.policy = load_policy(REPO / "policies" / "default.yaml", self.tenant)
        self.launches = 0
        self.confirm_posts = 0
        page.on("request", self._count)

    def _count(self, request: Any) -> None:
        if request.method == "POST" and "/review/" in request.url:
            self.confirm_posts += 1

    @contextmanager
    def _surface(self) -> Iterator[PlaywrightSurface]:
        self.launches += 1
        yield PlaywrightSurface(self.page)

    def approved(self, path: Path) -> Path:
        """A copy of a capability, approved and registered as `cua approve`
        would leave it: replay runs an approved file only if the ledger beside
        it records that approval."""
        cap = load(path)
        if cap.approval_state != "approved":
            cap = with_changes(cap, approval_state="approved", approved_by="test")
        copy = self.tmp / path.name
        Registry(self.tmp).register(save(cap, copy))
        return copy

    def consent(self, capability: Path, inputs: dict[str, str], *, by: str = "test") -> str:
        """A signed approval token for exactly this invocation."""
        return tokens.mint(
            load(capability), self.tenant, inputs, approved_by=by, key=SIGNING_KEY.encode()
        )

    def run(
        self,
        capability: Path,
        *,
        inputs: dict[str, str],
        inject: str | None = None,
        token: str | None = None,
        key: str | None = None,
        budget: Budget | None = None,
        screenshots: bool = False,
        environ: dict[str, str] | None = None,
    ) -> ReplayResult:
        return replay(
            self.approved(capability),
            tenant=self.tenant,
            policy=self.policy,
            invocation=Invocation(
                inputs=inputs,
                inject=inject,
                idempotency_key=key,
                approval=ApprovalGrant(token=token) if token else None,
                budget=budget or Budget(),
            ),
            runs_dir=self.runs,
            config=ReplayConfig(screenshots=screenshots),
            surface=self._surface,
            environ=environ or ENV,
        )

    def debug(self) -> dict[str, Any]:
        response = self.page.request.get(f"{self.base_url}/_debug/session")
        return dict(response.json())

    def events(self, result: ReplayResult) -> list[dict[str, Any]]:
        assert result.evidence.run_dir is not None
        log = Path(result.evidence.run_dir) / "log.jsonl"
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def harness(page: Page, mockapp_url: str, tmp_path: Path) -> Harness:
    return Harness(page, mockapp_url, tmp_path)


def balance(member_id: str) -> str:
    return str(MEMBERS[member_id].savings)


def goal2_inputs() -> dict[str, str]:
    return {"member_id": "10003", "initial_deposit": "250.00"}


def no_secret_in(run_dir: str | None) -> None:
    assert run_dir is not None
    for path in Path(run_dir).rglob("*"):
        if path.is_file() and path.suffix != ".png":
            data = path.read_bytes()
            if path.suffix == ".zip":
                with zipfile.ZipFile(path) as z:
                    data = b"".join(z.read(n) for n in z.namelist())
            assert f"F_PWD={PASSWORD}".encode() not in data, path
            assert f'"{PASSWORD}"'.encode() not in data, path


# -- row 1: the happy path ----------------------------------------------------


def test_row01_success_returns_the_seeded_balance(harness: Harness) -> None:
    result = harness.run(GOAL1, inputs={"member_id": "10003"}, screenshots=True)
    assert isinstance(result, Success), result
    assert Decimal(str(result.outputs["savings_balance"])) == MEMBERS["10003"].savings
    assert result.outputs["member_name"] == "Test Member 03"
    assert result.side_effect == "none"
    assert "bbox" not in result.locator_rungs_used.values()
    assert result.recoveries == [] and result.warnings == []
    assert result.evidence.screenshots, "every landed step leaves a masked screenshot"
    assert result.evidence.trace is None, "a trace is kept only for a failure"


# -- rows 2-4: business outcomes -------------------------------------------------


def test_row02_not_found_is_a_business_outcome_at_the_search(harness: Harness) -> None:
    result = harness.run(GOAL1, inputs={"member_id": "99999"}, inject="not_found")
    assert isinstance(result, BusinessOutcome), result
    assert result.code == "NOT_FOUND"
    assert result.step_id == "search.submit"
    assert result.message == "No matching member"


def test_row03_validation_error_names_the_field(harness: Harness) -> None:
    result = harness.run(
        GOAL2,
        inputs=goal2_inputs(),
        inject="validation_error",
        token=harness.consent(GOAL2, goal2_inputs()),
    )
    assert isinstance(result, BusinessOutcome), result
    assert result.code == "VALIDATION_ERROR"
    assert result.payload == {"message": "Initial deposit is required", "field": "initial_deposit"}
    assert result.side_effect == "none"
    assert harness.confirm_posts == 0


def test_row04_permission_denied_returns_what_could_be_read(harness: Harness) -> None:
    result = harness.run(GOAL1, inputs={"member_id": "10007"})
    assert isinstance(result, BusinessOutcome), result
    assert result.code == "PERMISSION_DENIED"
    assert result.outputs == {"member_name": "Test Member 07"}
    assert "savings_balance" not in result.outputs
    steps = [e["step"] for e in harness.events(result) if e["event"] == "step.start"]
    assert steps[-1] == "search.submit", "no step runs after the outcome is known"


# -- rows 5-7: recovered ---------------------------------------------------------


def test_row05_system_notice_is_dismissed_and_the_run_carries_on(harness: Harness) -> None:
    result = harness.run(GOAL1, inputs={"member_id": "10003"}, inject="interstitial_dialog")
    assert isinstance(result, Success), result
    assert result.outputs["savings_balance"] == balance("10003")
    assert [r.code for r in result.recoveries] == ["INTERSTITIAL"]
    assert result.recoveries[0].step_id == "login.submit"
    assert result.recoveries[0].action == "dismiss_notice"


def test_row06_a_slow_detail_page_is_waited_out(harness: Harness) -> None:
    started = time.monotonic()
    result = harness.run(GOAL1, inputs={"member_id": "10003"}, inject="slow_load")
    assert isinstance(result, Success), result
    assert result.outputs["savings_balance"] == balance("10003")
    assert result.recoveries[0].code == "SLOW_LOAD"
    assert result.recoveries[0].attempts >= 1
    assert time.monotonic() - started < 15


def test_row07_an_expired_session_signs_on_again_and_resumes(harness: Harness) -> None:
    result = harness.run(GOAL1, inputs={"member_id": "10003"}, inject="session_expired")
    assert isinstance(result, Success), result
    assert result.outputs["savings_balance"] == balance("10003")
    (recovery,) = result.recoveries
    assert recovery.code == "SESSION_EXPIRED"
    assert recovery.action == "relogin"
    assert recovery.resumed_after_checkpoint == "cp.logged_in"
    no_secret_in(result.evidence.run_dir)


# -- rows 8-11: failures -----------------------------------------------------------


def test_row08_a_server_error_is_a_failure_with_a_trace(harness: Harness) -> None:
    result = harness.run(GOAL1, inputs={"member_id": "10003"}, inject="server_error")
    assert isinstance(result, Failure), result
    assert result.code == "APP_ERROR"
    assert result.step_id == "search.submit"
    assert "HTTP 500" in result.message
    assert "/member/" in result.expected
    assert "Internal Server Error" in result.observed
    assert result.side_effect == "none"
    assert result.evidence.trace == "trace.zip"
    assert result.evidence.run_dir is not None
    assert (Path(result.evidence.run_dir) / "trace.zip").stat().st_size > 0
    no_secret_in(result.evidence.run_dir)


def test_row09_a_renamed_button_is_unresolved_not_guessed(harness: Harness) -> None:
    result = harness.run(GOAL1, inputs={"member_id": "10003"}, inject="renamed_button")
    assert isinstance(result, Failure), result
    assert result.code == "LOCATOR_UNRESOLVED"
    assert result.step_id == "search.submit"
    # Only the pixel rung still matched, and pixels alone are not acted on.
    assert "role_name: 0 matches" in result.message
    assert "not trusted" in result.message
    assert result.escalation_reason == "STUCK"
    assert 'button "Find"' in result.observed


def test_row10_a_second_search_button_elsewhere_does_not_confuse_the_step(
    harness: Harness,
) -> None:
    result = harness.run(GOAL1, inputs={"member_id": "10003"}, inject="ambiguous_button")
    assert isinstance(result, Success), result
    # The recorded rung is scoped to the main frame, so the nav frame's
    # "Search" is not a candidate at all.
    assert result.locator_rungs_used["search.submit"] == "role_name"


def test_row10_an_unscoped_rung_falls_through_on_ambiguity(harness: Harness) -> None:
    """The same inject against a ladder whose first rung is frame-blind: it
    matches two buttons, falls through, and the run says so."""
    cap = load(GOAL1)
    data = cap.model_dump(mode="json", by_alias=True)
    submit = next(s for s in data["steps"] if s["id"] == "search.submit")
    submit["target"] = [
        {"strategy": "role_name", "role": "button", "name": "Search"},
        {
            "strategy": "near_text",
            "text": "Member ID",
            "role": "button",
            "within": {"frame": "main"},
        },
    ]
    blind = harness.tmp / "blind" / "member_savings_balance.json"
    save(with_changes(cap, steps=data["steps"], content_sha256=None), blind)
    result = harness.run(blind, inputs={"member_id": "10003"}, inject="ambiguous_button")
    assert isinstance(result, Success), result
    assert result.locator_rungs_used["search.submit"] == "near_text"
    assert any("role_name (2 matches)" in w for w in result.warnings), result.warnings


def test_row11_a_slow_confirm_is_unknown_and_never_clicked_twice(harness: Harness) -> None:
    result = harness.run(
        GOAL2,
        inputs=goal2_inputs(),
        inject="slow_confirm",
        token=harness.consent(GOAL2, goal2_inputs()),
    )
    assert isinstance(result, Failure), result
    assert result.code == "TIMEOUT"
    assert result.side_effect == "unknown"
    assert result.step_id is not None and result.step_id.startswith("review.")
    assert harness.confirm_posts == 1
    # The write was still in flight when the engine gave up; it lands anyway,
    # which is exactly why "unknown" and not "none".
    time.sleep(SLOW_CONFIRM_SECONDS)
    assert harness.debug()["confirms"] == 1


# -- rows 12-13: detector scope, recovery cap ---------------------------------------


def test_row12_nothing_fires_on_a_clean_run(harness: Harness) -> None:
    result = harness.run(GOAL1, inputs={"member_id": "10003"})
    assert isinstance(result, Success), result
    assert not [e for e in harness.events(result) if e["event"] == "detector.matched"]


def test_row13_a_notice_that_keeps_coming_back_exhausts_recovery(harness: Harness) -> None:
    result = harness.run(GOAL1, inputs={"member_id": "10003"}, inject="interstitial_persistent")
    assert isinstance(result, Failure), result
    assert result.code == "RECOVERY_EXHAUSTED"
    cap = load(GOAL1)
    assert len(result.recoveries) == cap.recovery_limits.per_step
    assert result.escalation_reason == "UNRECOVERABLE"


# -- rows 14-15: approval ---------------------------------------------------------


def test_row15_a_valid_token_runs_the_commit_unattended(harness: Harness) -> None:
    token = harness.consent(GOAL2, goal2_inputs())
    result = harness.run(GOAL2, inputs=goal2_inputs(), token=token)
    assert isinstance(result, Success), result
    assert result.side_effect == "committed"
    assert str(result.outputs["reference_number"]).startswith("REF-10003-")
    assert harness.confirm_posts == 1
    approved = [e for e in harness.events(result) if e["event"] == "policy.approved"]
    assert approved and approved[0]["approved_by"] == "test"
    assert approved[0]["rule"] == "commit_on_review"
    run_dir = Path(str(result.evidence.run_dir))
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes(), path


def test_one_token_is_one_commit(harness: Harness) -> None:
    token = harness.consent(GOAL2, goal2_inputs())
    first = harness.run(GOAL2, inputs=goal2_inputs(), token=token)
    assert isinstance(first, Success), first
    again = harness.run(GOAL2, inputs=goal2_inputs(), token=token)
    assert isinstance(again, Failure) and again.code == "POLICY_BLOCKED"
    assert "already used" in again.message
    assert harness.launches == 1
    assert harness.debug()["confirms"] == 1


def test_consent_for_other_inputs_commits_nothing(harness: Harness) -> None:
    token = harness.consent(GOAL2, {"member_id": "10003", "initial_deposit": "5.00"})
    result = harness.run(GOAL2, inputs=goal2_inputs(), token=token)
    assert isinstance(result, Failure) and result.code == "POLICY_BLOCKED"
    assert "other inputs" in result.message
    assert harness.launches == 0 and harness.confirm_posts == 0


def test_row14_without_a_token_the_commit_is_not_made(harness: Harness) -> None:
    """M4 half of row 14: with no operator console yet, the step that needs
    approval is refused, and nothing has been committed. M6 turns this into an
    escalation."""
    result = harness.run(GOAL2, inputs=goal2_inputs())
    assert isinstance(result, Failure), result
    assert result.code == "POLICY_BLOCKED"
    assert result.escalation_reason == "NEEDS_APPROVAL"
    assert result.side_effect == "none"
    assert harness.confirm_posts == 0
    assert harness.debug()["confirms"] == 0


def test_an_unexpected_native_confirm_is_cancelled_and_reported(harness: Harness) -> None:
    result = harness.run(
        GOAL2,
        inputs=goal2_inputs(),
        inject="native_confirm",
        token=harness.consent(GOAL2, goal2_inputs()),
    )
    assert isinstance(result, Failure), result
    assert result.code == "ACTION_FAILED"
    assert "unexpected confirm" in result.message
    assert result.side_effect == "none"
    assert harness.debug()["confirms"] == 0


# -- rows 19-20: before the browser -----------------------------------------------


def test_row19_the_same_key_twice_runs_once(harness: Harness) -> None:
    first = harness.run(GOAL1, inputs={"member_id": "10003"}, key="agent-req-1")
    second = harness.run(GOAL1, inputs={"member_id": "10003"}, key="agent-req-1")
    assert harness.launches == 1
    assert isinstance(first, Success) and isinstance(second, Success)
    assert second.cached and not first.cached
    assert second.outputs == first.outputs
    reused = harness.run(GOAL1, inputs={"member_id": "10004"}, key="agent-req-1")
    assert isinstance(reused, Failure) and reused.code == "INPUT_INVALID"
    assert harness.launches == 1


def test_row20_bad_input_never_starts_a_browser(harness: Harness) -> None:
    result = harness.run(GOAL1, inputs={"member_id": "10O03"})
    assert isinstance(result, Failure), result
    assert result.code == "INPUT_INVALID"
    assert result.side_effect == "none"
    assert harness.launches == 0


def test_a_draft_is_refused_before_anything_runs(harness: Harness) -> None:
    cap = with_changes(load(GOAL1), approval_state="draft", approved_by=None, approved_at=None)
    draft = harness.tmp / "draft" / "member_savings_balance.json"
    save(cap, draft)
    result = replay(
        draft,
        tenant=harness.tenant,
        policy=harness.policy,
        invocation=Invocation(inputs={"member_id": "10003"}),
        runs_dir=harness.runs,
        surface=harness._surface,
        environ=ENV,
    )
    assert isinstance(result, Failure) and result.code == "POLICY_BLOCKED"
    assert "draft" in result.message
    assert harness.launches == 0


def test_the_budget_bounds_the_whole_run(harness: Harness) -> None:
    result = harness.run(
        GOAL1, inputs={"member_id": "10003"}, inject="slow_load", budget=Budget(timeout_s=2)
    )
    assert isinstance(result, Failure), result
    assert result.code == "TIMEOUT"


# -- states the plan did not enumerate ---------------------------------------------


def test_a_rotated_password_is_auth_failed_not_a_timeout(harness: Harness) -> None:
    """The most common way a working capability stops working in production."""
    result = harness.run(
        GOAL1,
        inputs={"member_id": "10003"},
        environ={"CUA_TEST_OPERATOR": "operator:no-longer-the-password"},
    )
    assert isinstance(result, Failure), result
    assert result.code == "AUTH_FAILED"
    assert result.step_id == "login.submit"
    assert result.side_effect == "none"
    assert "Invalid user id or password" in result.observed
    assert result.evidence.run_dir is not None
    for path in Path(result.evidence.run_dir).rglob("*"):
        if path.is_file() and path.suffix not in (".png", ".zip"):
            assert b"no-longer-the-password" not in path.read_bytes(), path


def test_three_replays_take_the_same_path_by_the_same_rungs(harness: Harness) -> None:
    """Determinism as evidence rather than as a sentence: the same artifact,
    replayed three times, passes the same steps in the same order, names every
    control by the same rung, and returns the same outputs."""
    traces = []
    for _ in range(3):
        result = harness.run(GOAL1, inputs={"member_id": "10003"})
        assert isinstance(result, Success), result
        passed = [e["step"] for e in harness.events(result) if e["event"] == "step.passed"]
        traces.append((passed, result.locator_rungs_used, result.outputs, result.recoveries))
    assert traces[0] == traces[1] == traces[2]
    assert traces[0][0] == [s.id for s in load(GOAL1).steps]
    assert harness.launches == 3


def test_the_field_a_credential_was_typed_into_is_masked_in_every_later_screenshot(
    harness: Harness,
) -> None:
    result = harness.run(GOAL1, inputs={"member_id": "10003"}, screenshots=True)
    assert isinstance(result, Success), result
    added = [e for e in harness.events(result) if e["event"] == "screenshot.mask_added"]
    assert [e["step"] for e in added] == ["login.username", "login.password"]
    assert any("User ID" in e["target"] for e in added)
