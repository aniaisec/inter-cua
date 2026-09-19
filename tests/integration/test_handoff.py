"""Handoff on the live session, against the real mock app and the real console.

A replay runs in the test's own page, with the handoff channel on. The
operator console runs as a server on a free port, over the same runs
directory. The "person" is a thread that behaves like one: it watches the
console's API for a request, takes control, works the browser over CDP with a
Playwright connection of its own — a second party attached to the very page
the replay is driving — and then decides on the console.

Plan rows 9 and 14-18, the M6 cases (the person dismisses a dialog; the person
finishes the flow), and the async path: a replay that returns ``escalated``,
exits, and is carried on by ``cua resume`` in another process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from playwright.sync_api import Page, sync_playwright

from cua.escalation.capture import find_page
from cua.escalation.channel import HandoffSettings
from cua.escalation.controller import ControlStore
from cua.escalation.operator_app import create_app
from cua.replay.engine import ReplayConfig
from cua.replay.invocation import ApprovalGrant, Budget, Invocation
from cua.replay.result import Escalated, Failure, ReplayResult, Success
from cua.replay.runner import ResumeContext, replay, resume
from cua.surface.playwright_surface import BrowserProcess, PlaywrightSurface
from tests.conftest import REPO_ROOT, free_port
from tests.integration.conftest import BrowserSession
from tests.integration.test_replay import (
    ENV,
    GOAL1,
    GOAL2,
    Harness,
    goal2_inputs,
    no_secret_in,
)

pytestmark = pytest.mark.browser

REQUEST_TIMEOUT_S = 60.0


# --------------------------------------------------------------------------
# The console, and a person at it
# --------------------------------------------------------------------------


@pytest.fixture
def console(tmp_path: Path) -> Iterator[str]:
    """The operator console over this test's runs directory, in a thread."""
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(tmp_path / "runs"), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/api/requests", timeout=1).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.05)
    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class Person(threading.Thread):
    """Someone at the console: waits for a request, maybe takes control and
    does something in the browser, then decides."""

    def __init__(
        self,
        console: str,
        *,
        decide: str,
        act: Callable[[Page], None] | None = None,
        take_control: bool = True,
        why: str = "",
    ) -> None:
        super().__init__(daemon=True)
        self.console = console
        self.decide = decide
        self.act = act
        self.take_control = take_control
        self.why = why
        self.error: BaseException | None = None
        self.request: dict[str, Any] = {}

    def run(self) -> None:
        try:
            self._run()
        except BaseException as exc:  # reported by finish()
            self.error = exc

    def _run(self) -> None:
        with httpx.Client(base_url=self.console, timeout=30) as http:
            deadline = time.monotonic() + REQUEST_TIMEOUT_S
            while True:
                open_ = [v for v in http.get("/api/requests").json() if v["state"] == "PAUSED"]
                if open_:
                    break
                assert time.monotonic() < deadline, "no request was opened"
                time.sleep(0.1)
            self.request = open_[0]["request"]
            rid = self.request["id"]
            if self.take_control:
                taken = http.post(f"/api/requests/{rid}/take_control", json={"by": "pat"})
                assert taken.status_code == 200, taken.text
                assert taken.json()["holder"] == "human"
            if self.act is not None:
                with sync_playwright() as pw:
                    browser = pw.chromium.connect_over_cdp(self.request["cdp_url"])
                    try:
                        page = find_page(browser.contexts, self.request["target_id"])
                        assert page is not None, "the run's page is not in the browser"
                        self.act(page)
                        page.wait_for_timeout(300)  # let the capture see the last event
                    finally:
                        browser.close()
            decided = http.post(
                f"/api/requests/{rid}/{self.decide}", json={"by": "pat", "why": self.why}
            )
            assert decided.status_code == 200, decided.text

    def finish(self) -> None:
        self.join(timeout=REQUEST_TIMEOUT_S)
        assert not self.is_alive(), "the person never finished"
        if self.error is not None:
            raise self.error


def main_frame(page: Page) -> Any:
    frame = page.frame(name="main")
    assert frame is not None
    return frame


def click_find(page: Page) -> None:
    main = main_frame(page)
    main.get_by_role("button", name="Find").click()
    main.wait_for_url("**/member/**")


def click_confirm(page: Page) -> None:
    main = main_frame(page)
    main.get_by_role("button", name="Confirm").click()
    main.get_by_text("Sub-account Opened").wait_for()


def dismiss_notice(page: Page) -> None:
    main_frame(page).get_by_role("button", name="OK").click()


# --------------------------------------------------------------------------
# A replay with the channel on
# --------------------------------------------------------------------------


class HandoffHarness(Harness):
    def __init__(self, page: Page, base_url: str, tmp_path: Path, cdp_url: str) -> None:
        super().__init__(page, base_url, tmp_path)
        self.cdp_url = cdp_url

    @contextmanager
    def _surface(self) -> Iterator[PlaywrightSurface]:
        self.launches += 1
        yield PlaywrightSurface(self.page, cdp_url=self.cdp_url)

    def handoff(
        self,
        capability: Path,
        *,
        console: str,
        inputs: dict[str, str],
        inject: str | None = None,
        token: str | None = None,
        wait_s: float = REQUEST_TIMEOUT_S,
        budget: Budget | None = None,
    ) -> ReplayResult:
        return replay(
            self.approved(capability),
            tenant=self.tenant,
            policy=self.policy,
            invocation=Invocation(
                inputs=inputs,
                inject=inject,
                approval=ApprovalGrant(token=token) if token else None,
                budget=budget or Budget(),
            ),
            runs_dir=self.runs,
            config=ReplayConfig(screenshots=False),
            surface=self._surface,
            environ=ENV,
            handoff=HandoffSettings(wait_s=wait_s, operator_url=console, poll_s=0.1),
        )

    def run_dir(self, result: ReplayResult) -> Path:
        assert result.evidence.run_dir is not None
        return Path(result.evidence.run_dir)

    def transitions(self, result: ReplayResult) -> list[tuple[str | None, str]]:
        return [(e["from"], e["to"]) for e in ControlStore(self.run_dir(result)).transitions()]  # type: ignore[misc]

    def human_actions(self, result: ReplayResult) -> list[dict[str, Any]]:
        path = self.run_dir(result) / "human_actions.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.is_file() else []


@pytest.fixture
def h(
    page: Page, mockapp_url: str, tmp_path: Path, browser_session: BrowserSession
) -> HandoffHarness:
    return HandoffHarness(page, mockapp_url, tmp_path, browser_session.cdp_url)


def steps_after_handback(h: HandoffHarness, result: ReplayResult) -> list[str]:
    events = h.events(result)
    resumed = max(i for i, e in enumerate(events) if e["event"] == "handoff.resumed")
    return [str(e["step"]) for e in events[resumed:] if e["event"] == "step.start"]


# -- row 9: a renamed button is asked about, with everything a person needs ------------


def test_row09_a_renamed_button_escalates_with_a_masked_screenshot(
    h: HandoffHarness, console: str
) -> None:
    result = h.handoff(
        GOAL1, console=console, inputs={"member_id": "10003"}, inject="renamed_button", wait_s=0
    )
    assert isinstance(result, Escalated), result
    assert (result.reason, result.step_id, result.side_effect) == ("STUCK", "search.submit", "none")
    assert result.resume_token.startswith("rsm_")
    assert result.operator_url and result.operator_url.startswith(console)

    assert result.evidence.intervention is not None
    request = json.loads((h.run_dir(result) / result.evidence.intervention).read_text())
    assert (request["reason_code"], request["code"], request["step_id"]) == (
        "STUCK",
        "LOCATOR_UNRESOLVED",
        "search.submit",
    )
    shot = h.run_dir(result) / request["masked_screenshot"]
    assert shot.read_bytes().startswith(b"\x89PNG")
    assert 'button "Find"' in request["a11y_excerpt"]
    assert request["cdp_url"] and request["target_id"]
    listed = httpx.get(f"{console}/api/requests").json()
    assert [v["state"] for v in listed] == ["PAUSED"]
    no_secret_in(result.evidence.run_dir)


# -- row 16, and the async path: the person finishes, `resume` carries on ---------------


def test_row16_the_person_finishes_and_resume_carries_on_from_cp_done(
    h: HandoffHarness, console: str
) -> None:
    """Escalated and returned; a person takes over and presses Find; the
    caller then resumes with its token. The screen proves the run is done."""
    first = h.handoff(
        GOAL1, console=console, inputs={"member_id": "10003"}, inject="renamed_button", wait_s=0
    )
    assert isinstance(first, Escalated), first
    person = Person(console, decide="resume", act=click_find)
    person.start()
    person.finish()

    @contextmanager
    def same_page(_: ResumeContext) -> Iterator[PlaywrightSurface]:
        with h._surface() as surface:
            yield surface

    result = resume(first.resume_token, runs_dir=h.runs, surface=same_page, environ=ENV)
    assert isinstance(result, Success), result
    assert result.outputs["savings_balance"] == "1411.21"
    (handed,) = result.handoffs
    assert (handed.decision, handed.resumed_at, handed.resumed_after_checkpoint) == (
        "hand_back",
        "done",
        "cp.done",
    )
    assert handed.human_actions_count >= 2  # the click, the navigation
    assert steps_after_handback(h, result) == []  # nothing was done again
    actions = h.human_actions(result)
    assert any(a.get("action") == "click" and a["target"]["name"] == "Find" for a in actions)
    assert not any("10003" in json.dumps(a.get("target", {})) for a in actions)  # no values
    assert h.transitions(result)[-1] == ("RESUMING", "AUTOMATION")
    again = resume(first.resume_token, runs_dir=h.runs, surface=same_page, environ=ENV)
    assert isinstance(again, Success) and again.cached  # the answer, not a second run


def test_the_waiting_replay_carries_on_in_process_when_the_person_hands_back(
    h: HandoffHarness, console: str
) -> None:
    person = Person(console, decide="resume", act=click_find)
    person.start()
    result = h.handoff(
        GOAL1, console=console, inputs={"member_id": "10003"}, inject="renamed_button"
    )
    person.finish()
    assert isinstance(result, Success), result
    assert result.handoffs[0].human_actions_count >= 1
    assert h.transitions(result) == [
        (None, "AUTOMATION"),
        ("AUTOMATION", "PAUSED"),
        ("PAUSED", "HUMAN_IN_CONTROL"),
        ("HUMAN_IN_CONTROL", "RESUMING"),
        ("RESUMING", "AUTOMATION"),
    ]


# -- row 14: no token, the person approves ---------------------------------------------


def test_row14_without_a_token_the_person_approves_and_the_commit_runs_once(
    h: HandoffHarness, console: str
) -> None:
    person = Person(console, decide="approve", take_control=False)
    person.start()
    result = h.handoff(GOAL2, console=console, inputs=goal2_inputs())
    person.finish()

    assert isinstance(result, Success), result
    assert result.side_effect == "committed"
    assert str(result.outputs["reference_number"]).startswith("REF-10003-")
    assert h.confirm_posts == 1 and h.debug()["confirms"] == 1
    (handed,) = result.handoffs
    assert (handed.reason, handed.decision, handed.decided_by) == (
        "NEEDS_APPROVAL",
        "approve",
        "pat",
    )
    assert any(a["action"] == "approve" for a in h.human_actions(result))
    approved = [e for e in h.events(result) if e["event"] == "policy.approved"]
    assert approved[0]["via"] == "console"


# -- the person completes the rest of the flow themselves ---------------------------


def test_the_person_commits_and_the_run_never_presses_confirm(
    h: HandoffHarness, console: str
) -> None:
    person = Person(console, decide="resume", act=click_confirm)
    person.start()
    result = h.handoff(GOAL2, console=console, inputs=goal2_inputs())
    person.finish()

    assert isinstance(result, Success), result
    assert result.side_effect == "committed"
    assert str(result.outputs["reference_number"]).startswith("REF-10003-")
    assert h.confirm_posts == 1  # the person's
    assert not [e for e in h.events(result) if e["event"] == "irreversible.act"]
    committed = [e for e in h.events(result) if e["event"] == "irreversible.committed"]
    assert committed[0]["performed_by"] == "person"
    assert result.handoffs[0].resumed_after_checkpoint == "cp.done"
    assert steps_after_handback(h, result) == []


# -- a dialog the capability does not know about ------------------------------------


def test_the_person_dismisses_an_overlay_and_the_run_carries_on(
    h: HandoffHarness, console: str
) -> None:
    person = Person(console, decide="resume", act=dismiss_notice)
    person.start()
    result = h.handoff(
        GOAL2,
        console=console,
        inputs=goal2_inputs(),
        inject="modal_dialog",
        token=h.consent(GOAL2, goal2_inputs()),
    )
    person.finish()

    assert isinstance(result, Success), result
    assert result.side_effect == "committed"
    (handed,) = result.handoffs
    assert handed.step_id == "member.open_sub_account"
    assert handed.human_actions_count >= 1
    assert handed.resumed_after_checkpoint == "cp.member_detail"
    assert steps_after_handback(h, result)[0] == "member.open_sub_account"
    assert person.request["code"] == "ACTION_FAILED"


# -- rows 17-18: aborted escalations ------------------------------------------------


def test_row17_allow_escalation_false_aborts_without_asking(
    h: HandoffHarness, console: str
) -> None:
    result = h.handoff(
        GOAL1,
        console=console,
        inputs={"member_id": "10003"},
        inject="server_error",
        budget=Budget(allow_escalation=False),
    )
    assert isinstance(result, Failure), result
    assert (result.code, result.escalation_reason) == ("ESCALATION_ABORTED", "UNRECOVERABLE")
    assert "APP_ERROR" in result.message
    assert not (h.run_dir(result) / "interventions").exists()
    assert httpx.get(f"{console}/api/requests").json() == []


def test_row18_the_person_aborts_a_server_error(h: HandoffHarness, console: str) -> None:
    person = Person(console, decide="abort", why="core is down")
    person.start()
    result = h.handoff(GOAL1, console=console, inputs={"member_id": "10003"}, inject="server_error")
    person.finish()

    assert isinstance(result, Failure), result
    assert (result.code, result.side_effect) == ("ESCALATION_ABORTED", "none")
    assert "pat aborted the run at search.submit" in result.message
    assert "core is down" in result.message
    assert person.request["code"] == "APP_ERROR"
    assert h.transitions(result)[-1] == ("HUMAN_IN_CONTROL", "ABORTED")
    assert result.evidence.trace == "trace.zip"


# -- the async path across processes, with a browser that outlives them ---------------


def test_an_escalated_cli_run_exits_and_cua_resume_finishes_it(
    mockapp_url: str, tmp_path: Path, console: str
) -> None:
    tenant = tmp_path / "tenant.yaml"
    tenant.write_text(
        (REPO_ROOT / "tenants" / "local.yaml")
        .read_text()
        .replace("http://127.0.0.1:8000", mockapp_url)
    )
    env = {**os.environ, "CUA_SECRET_MOCKCORE_OPERATOR": "operator:operator"}
    cua = [sys.executable, "-m", "cua.cli"]
    runs = ["--runs-dir", str(tmp_path / "runs")]
    first = subprocess.run(
        [
            *cua,
            "replay",
            str(GOAL1),
            "--tenant",
            str(tenant),
            "--input",
            "member_id=10003",
            "--inject",
            "renamed_button",
            "--handoff",
            "--handoff-wait",
            "0",
            "--operator-url",
            console,
            *runs,
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert first.returncode == 3, first.stderr
    escalated = json.loads(first.stdout)
    session = json.loads((Path(escalated["evidence"]["run_dir"]) / "session.json").read_text())
    browser = BrowserProcess(session["pid"], session["cdp_url"], Path(session["profile"]))
    try:
        assert browser.alive()  # the replay process is gone; its session is not

        person = Person(console, decide="resume", act=click_find)
        person.start()
        person.finish()

        second = subprocess.run(
            [*cua, "resume", escalated["resume_token"], *runs],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
            timeout=120,
        )
        assert second.returncode == 0, second.stderr
        done = json.loads(second.stdout)
        assert done["kind"] == "success" and done["outputs"]["savings_balance"] == "1411.21"
        assert done["handoffs"][0]["resumed_after_checkpoint"] == "cp.done"
        assert not browser.alive()  # the finished run closed its session
    finally:
        browser.kill()


# -- discovery: the commit's approval flows through the console ------------------------


def test_discovery_of_goal_two_is_approved_on_the_console_and_records(
    h: HandoffHarness, console: str, tmp_path: Path
) -> None:
    from cua.agent.goal import Goal, OutputSpec, ParamSpec
    from cua.agent.llm import ScriptedClient
    from cua.agent.loop import DiscoveryLoop
    from cua.agent.script import load_script
    from cua.artifact.recorder import record
    from cua.escalation.channel import OperatorChannel
    from cua.escalation.lease import LeasedSurface
    from cua.evidence.logger import RunLog
    from cua.secrets.resolver import resolve

    credential = resolve("secret://local/mockcore/operator", h.tenant, environ=ENV)
    goal = Goal(
        goal="Open a savings sub-account for a member with an initial deposit",
        name="open_subaccount",
        entry="/login",
        params=[
            ParamSpec(name="member_id", value="10003"),
            ParamSpec(name="initial_deposit", type="decimal", value="250.00"),
        ],
        outputs=[OutputSpec(name="reference_number")],
        credentials={"app_login": credential.ref},
    )
    log = RunLog.create(h.runs)
    control = ControlStore(log.dir)
    control.start(log.run_id)
    live = PlaywrightSurface(h.page, cdp_url=h.cdp_url)
    channel = OperatorChannel(
        log=log,
        runs_dir=h.runs,
        control=control,
        session=live.expose,
        kind="discovery",
        capability=goal.name,
        capability_version=None,
        tenant=h.tenant.id,
        settings=HandoffSettings(wait_s=REQUEST_TIMEOUT_S, operator_url=console, poll_s=0.1),
    )
    person = Person(console, decide="approve", take_control=False)
    person.start()
    outcome = DiscoveryLoop(
        surface=LeasedSurface(live, control.lease),
        llm=ScriptedClient(load_script(REPO_ROOT / "scripts/discovery/open_subaccount.yaml")),
        goal=goal,
        tenant=h.tenant,
        policy=h.policy,
        credentials={"app_login": credential},
        log=log,
        handoff=channel,
    ).run()
    person.finish()

    assert outcome.kind == "done", outcome
    assert outcome.outputs["reference_number"].normalized.startswith("REF-10003-")
    assert [x.decision for x in outcome.handoffs] == ["approve"]
    assert not outcome.human_assisted
    assert person.request["kind"] == "discovery"
    assert person.request["reason_code"] == "NEEDS_APPROVAL"
    assert h.confirm_posts == 1
    cap = record(log.dir, policy=h.policy, families_dir=REPO_ROOT / "capabilities" / "families")
    confirm = next(s for s in cap.steps if s.risk == "irreversible")
    assert confirm.approval == "required"
