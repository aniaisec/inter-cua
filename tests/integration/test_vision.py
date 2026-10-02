"""The vision fallback against the real mock app.

The capabilities here are the committed discovery runs recorded again at
schema 1.3, which gives each click step its recorded appearance, then approved
in the test's own directory. The committed v3 artifacts carry none, and are
replayed too, to show they behave exactly as before.

``hidden_control`` draws the Search and Confirm buttons as always and hides
them from the accessibility tree; ``hidden_duplicate`` adds a second, identical
hidden Search button. What the mock app records (a search posted, a sub-account
opened) is asserted alongside the typed result.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page

from cua.artifact.recorder import record
from cua.artifact.store import save
from cua.policy.allowlist import Policy, VisionRules, load_policy
from cua.replay.engine import ReplayConfig
from cua.replay.invocation import ApprovalGrant, Invocation
from cua.replay.result import Failure, ReplayResult, Success
from cua.replay.runner import replay
from cua.surface.conditions import RegionPresent
from cua.surface.playwright_surface import PlaywrightSurface
from cua.surface.protocol import ClickPoint, Observation, PerceptionDrift, Rect, StaleRefError
from cua.surface.vision import image
from cua.surface.vision.candidate import Appearance
from cua.tenant import load_tenant
from mockapp.injects import Inject
from tests.integration.test_replay import ENV, GOAL1, REPO, Harness, goal2_inputs
from tests.integration.test_surface import MEMBER_ID, fill, sign_on

pytestmark = pytest.mark.browser

EVIDENCE = REPO / "evidence"
POLICY = REPO / "policies" / "default.yaml"


class VisionHarness(Harness):
    def recorded(self, discovery: str) -> Path:
        """The discovery run recorded at schema 1.3, approved here. Recorded
        under the policy of the tenant the run was made on, whose origin is
        the one its screens were on."""
        cap = record(
            EVIDENCE / discovery,
            policy=load_policy(POLICY, load_tenant("local", root=REPO / "tenants")),
        )
        # Committed screenshots were captured on Windows. Native controls and
        # fonts render differently on Linux, so record fixture appearances on
        # the same platform as replay, without lowering the match threshold.
        surface = PlaywrightSurface(self.page)
        sign_on(surface, self.base_url)
        pictures = {"search.submit": self.picture(surface, "Search")}
        if cap.name == "open_subaccount":
            response = self.page.request.post(
                f"{self.base_url}/subaccount/10003",
                form={"F_ACCTTYP": "Savings", "F_DEPAMT": "250.00"},
            )
            assert response.ok
            self.page.goto(f"{self.base_url}/review/10003")
            pictures["review.submit"] = self.picture(surface, "Confirm")
        assert all(step.appearance is not None for step in cap.steps if step.id in pictures)
        cap = cap.model_copy(
            update={
                "steps": [
                    step.model_copy(update={"appearance": pictures[step.id]})
                    if step.id in pictures
                    else step
                    for step in cap.steps
                ]
            }
        )
        path = self.tmp / "recorded" / f"{cap.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        save(cap, path)
        return self.approved(path)

    @staticmethod
    def picture(surface: PlaywrightSurface, name: str) -> Appearance:
        screen = surface.observe(screenshot=True)
        assert screen.screenshot_png is not None
        nodes = [node for node in screen.nodes if node.role == "button" and node.name == name]
        assert len(nodes) == 1
        box = nodes[0].bbox
        assert box is not None
        scale = image.scale_of(screen.screenshot_png, screen.viewport)
        pixels = Rect(x=box.x * scale, y=box.y * scale, w=box.w * scale, h=box.h * scale)
        crop = image.crop(screen.screenshot_png, pixels)
        w, h = image.size_of(crop)
        return Appearance.of(crop, w=w, h=h, scale=scale)

    def replay(
        self,
        capability: Path,
        *,
        inputs: dict[str, str],
        inject: str | None = None,
        token: str | None = None,
        vision: bool = True,
        policy: Policy | None = None,
    ) -> ReplayResult:
        return replay(
            self.approved(capability),
            tenant=self.tenant,
            policy=policy or self.policy,
            invocation=Invocation(
                inputs=inputs,
                inject=inject,
                approval=ApprovalGrant(token=token) if token else None,
            ),
            runs_dir=self.runs,
            config=ReplayConfig(screenshots=False, vision=vision),
            surface=self._surface,
            environ=ENV,
        )

    def vision_events(self, result: ReplayResult) -> dict[str, list[dict[str, object]]]:
        out: dict[str, list[dict[str, object]]] = {}
        for e in self.events(result):
            if str(e["event"]).startswith("vision."):
                out.setdefault(str(e["event"]), []).append(e)
        return out


@pytest.fixture
def harness(page: Page, mockapp_url: str, tmp_path: Path) -> VisionHarness:
    return VisionHarness(page, mockapp_url, tmp_path)


@pytest.fixture
def goal1(harness: VisionHarness) -> Path:
    return harness.recorded("discovery-member-savings-balance")


def test_recording_again_gives_each_click_an_appearance(goal1: Path) -> None:
    from cua.artifact.store import load

    cap = load(goal1)
    assert cap.schema_version == "1.3"
    with_picture = [s.id for s in cap.steps if s.appearance is not None]
    assert with_picture == ["login.submit", "search.submit"]
    assert all(s.action == "click" for s in cap.steps if s.appearance is not None)


def test_a_hidden_control_is_found_by_its_picture_and_the_run_succeeds(
    harness: VisionHarness, goal1: Path
) -> None:
    result = harness.replay(goal1, inputs={"member_id": "10003"}, inject="hidden_control")
    assert isinstance(result, Success), result
    assert result.outputs["savings_balance"] == "1411.21"
    assert result.locator_rungs_used["search.submit"] == "vision"
    assert result.locator_rungs_used["login.submit"] == "role_name"
    assert any("search.submit: located by vision" in w for w in result.warnings)

    events = harness.vision_events(result)
    [accepted] = events["vision.accepted"]
    assert accepted["confidence"] == 1.0
    shot = Path(str(result.evidence.run_dir)) / str(accepted["evidence"])
    assert shot.is_file() and shot.read_bytes().startswith(b"\x89PNG")
    # The step's own expectation and checkpoint still decided that it landed.
    assert any(
        e["event"] == "checkpoint.passed" and e["step"] == "search.submit"
        for e in harness.events(result)
    )


def test_without_the_flag_a_hidden_control_is_unresolved_as_before(
    harness: VisionHarness, goal1: Path
) -> None:
    result = harness.replay(
        goal1, inputs={"member_id": "10003"}, inject="hidden_control", vision=False
    )
    assert isinstance(result, Failure), result
    assert result.code == "LOCATOR_UNRESOLVED" and result.step_id == "search.submit"
    assert "vision" not in result.message
    assert not harness.vision_events(result)


def test_a_policy_that_does_not_allow_vision_turns_it_off(
    harness: VisionHarness, goal1: Path
) -> None:
    closed = harness.policy.model_copy(update={"vision": VisionRules(allowed=False)})
    result = harness.replay(
        goal1, inputs={"member_id": "10003"}, inject="hidden_control", policy=closed
    )
    assert isinstance(result, Failure), result
    assert result.code == "LOCATOR_UNRESOLVED"
    assert any("policy does not allow it" in w for w in result.warnings)


def test_a_capability_with_no_appearance_has_no_fallback(harness: VisionHarness) -> None:
    result = harness.replay(GOAL1, inputs={"member_id": "10003"}, inject="hidden_control")
    assert isinstance(result, Failure), result
    assert result.code == "LOCATOR_UNRESOLVED"
    assert not harness.vision_events(result)


def test_two_places_that_look_alike_are_escalated_not_chosen(
    harness: VisionHarness, goal1: Path
) -> None:
    result = harness.replay(goal1, inputs={"member_id": "10003"}, inject="hidden_duplicate")
    assert isinstance(result, Failure), result
    assert result.code == "LOCATOR_AMBIGUOUS"
    assert result.escalation_reason == "STUCK"
    assert "vision (ambiguous): 2 places look like the recorded control" in result.message
    assert result.side_effect == "none"
    assert "Balances" not in result.observed  # nothing was clicked
    [refused] = harness.vision_events(result)["vision.refused"]
    assert refused["why"] == "ambiguous"


def test_a_renamed_control_does_not_look_like_its_picture(
    harness: VisionHarness, goal1: Path
) -> None:
    result = harness.replay(goal1, inputs={"member_id": "10003"}, inject="renamed_button")
    assert isinstance(result, Failure), result
    assert result.code == "LOCATOR_UNRESOLVED"
    assert "vision (not_found)" in result.message
    [candidates] = harness.vision_events(result)["vision.candidates"]
    assert all(float(str(m["confidence"])) < 0.97 for m in candidates["matches"])  # type: ignore[union-attr]


def test_a_commit_is_never_made_by_pixels_even_with_consent(harness: VisionHarness) -> None:
    goal2 = harness.recorded("discovery-open-subaccount")
    token = harness.consent(goal2, goal2_inputs())
    result = harness.replay(goal2, inputs=goal2_inputs(), inject="hidden_control", token=token)
    assert isinstance(result, Failure), result
    assert result.code == "POLICY_BLOCKED"
    assert result.step_id is not None and result.step_id.startswith("review.")
    assert "vision (unsafe)" in result.message and "irreversible" in result.message
    assert result.escalation_reason == "STUCK"
    assert result.side_effect == "none"
    assert harness.confirm_posts == 0
    assert harness.debug()["confirms"] == 0


# -- the pointer guard, on the surface itself ---------------------------------


def _hidden_search_point(surface: PlaywrightSurface) -> tuple[ClickPoint, Observation]:
    """The hidden Search button, found as vision would find it."""
    screen = surface.observe(screenshot=True)
    assert screen.screenshot_png is not None
    box = Rect(x=354, y=64, w=63, h=20)
    point = ClickPoint(
        x=box.center[0],
        y=box.center[1],
        guard=box,
        guard_sha256=image.pixels_sha256(screen.screenshot_png, box),
        role="button",
        name="Search",
        frame="main",
    )
    return point, screen


def test_a_point_is_clicked_when_its_pixels_are_unchanged(page: Page, mockapp_url: str) -> None:
    surface = PlaywrightSurface(page)
    sign_on(surface, mockapp_url, inject=Inject.HIDDEN_CONTROL)
    fill(surface, MEMBER_ID, "10003")
    point, _ = _hidden_search_point(surface)
    surface.act(point)
    surface.wait_for(RegionPresent(name="Balances"), 10.0)


def test_a_point_whose_pixels_changed_is_not_clicked(page: Page, mockapp_url: str) -> None:
    surface = PlaywrightSurface(page)
    sign_on(surface, mockapp_url, inject=Inject.HIDDEN_CONTROL)
    fill(surface, MEMBER_ID, "10003")
    point, _ = _hidden_search_point(surface)
    main = page.frame(name="main")
    assert main is not None
    main.evaluate("document.querySelector('input[type=submit]').value = 'Delete'")
    with pytest.raises(PerceptionDrift, match="changed after the point was chosen"):
        surface.act(point)
    assert main.url.endswith("/search")  # nothing was submitted


def test_a_point_outlives_its_observation_as_a_ref_does(page: Page, mockapp_url: str) -> None:
    surface = PlaywrightSurface(page)
    sign_on(surface, mockapp_url, inject=Inject.HIDDEN_CONTROL)
    point, _ = _hidden_search_point(surface)
    fill(surface, MEMBER_ID, "10003")  # acting clears the observation the point came from
    with pytest.raises(StaleRefError):
        surface.act(point)
