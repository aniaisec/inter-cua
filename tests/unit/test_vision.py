"""The vision fallback's parts, without a browser: the detector on synthetic
pictures, every refusal the validator makes, the policy's reading of a click
by pixels, the requirements it adds to a run, the schema 1.3 field, the
recorder's crops, and the drift it is reported as."""

from __future__ import annotations

import base64
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from cua.artifact import requirements
from cua.artifact.recorder import record
from cua.artifact.schema import Capability, Step
from cua.drift.detect import read_drift
from cua.policy.allowlist import Allow, Block, NeedsApproval, Policy, check, load_policy
from cua.surface.adapters import PLAYWRIGHT, WINDOWS_UIA
from cua.surface.locators import NearText, RoleName, Within
from cua.surface.protocol import (
    ClickPoint,
    FrameInfo,
    Node,
    Observation,
    Rect,
    Viewport,
)
from cua.surface.vision import image
from cua.surface.vision.candidate import Appearance
from cua.surface.vision.detector import TemplateDetector, VisualMatch, _fast_len, ncc
from cua.surface.vision.validator import Accepted, Refused, StepClaim, validate
from cua.tenant import Tenant
from tests.conftest import REPO_ROOT
from tests.unit.test_drift import built_run, resolved, step

TENANT = Tenant(id="local", app_family="legacy-core", base_url="http://127.0.0.1:8000")
POLICY = load_policy(REPO_ROOT / "policies" / "default.yaml", TENANT)
SEARCH = "http://127.0.0.1:8000/search"
REVIEW = "http://127.0.0.1:8000/review/10003"
BUTTON = Rect(x=60, y=30, w=40, h=16)


# -- pictures ----------------------------------------------------------------


def _png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _button(label: str = "Go") -> Image.Image:
    img = Image.new("RGB", (int(BUTTON.w), int(BUTTON.h)), (212, 208, 200))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, BUTTON.w - 1, BUTTON.h - 1], outline=(64, 64, 64))
    draw.text((8, 2), label, fill=(0, 0, 0))
    return img


def _screen(*at: tuple[int, int], size: tuple[int, int] = (200, 100), label: str = "Go") -> bytes:
    img = Image.new("RGB", size, (255, 255, 255))
    for x, y in at:
        img.paste(_button(label), (x, y))
    return _png(img)


def _appearance(label: str = "Go", scale: float = 1.0) -> Appearance:
    png = _png(_button(label))
    return Appearance.of(png, w=int(BUTTON.w), h=int(BUTTON.h), scale=scale)


def _observation(url: str = SEARCH, nodes: list[Node] | None = None) -> Observation:
    return Observation(
        observed_at=datetime.now(UTC),
        location=url,
        title="t",
        viewport=Viewport(w=200, h=100),
        frames=[FrameInfo(name="", url=url)],
        nodes=nodes or [],
    )


# -- the detector ------------------------------------------------------------------


def test_the_same_picture_is_found_once_where_it_is() -> None:
    [match] = TemplateDetector().detect(_screen((60, 30)), _appearance())
    assert match.confidence == pytest.approx(1.0)
    assert (match.bounds.x, match.bounds.y, match.bounds.w, match.bounds.h) == (60, 30, 40, 16)


def test_two_copies_are_two_matches_and_neither_is_preferred() -> None:
    matches = TemplateDetector().detect(_screen((10, 10), (120, 60)), _appearance())
    assert sorted((m.bounds.x, m.bounds.y) for m in matches) == [(10, 10), (120, 60)]
    assert matches[0].confidence == pytest.approx(matches[1].confidence)


def test_a_differently_labelled_control_scores_below_any_threshold() -> None:
    matches = TemplateDetector().detect(_screen((60, 30), label="No"), _appearance("Go"))
    assert all(m.confidence < POLICY.vision.min_confidence for m in matches)


def test_a_blank_screen_or_a_template_larger_than_it_matches_nothing() -> None:
    assert TemplateDetector().detect(_screen(), _appearance()) == []
    assert TemplateDetector().detect(_screen(size=(30, 10)), _appearance()) == []
    flat = image.gray(_png(Image.new("RGB", (8, 8), (9, 9, 9))))
    assert not ncc(image.gray(_screen((60, 30))), flat).any()


def test_fft_lengths_have_no_large_prime_factor() -> None:
    for n in (1, 7, 819, 1341, 1299):
        length = _fast_len(n)
        assert length >= n
        for p in (2, 3, 5):
            while length % p == 0:
                length //= p
        assert length == 1


# -- the recorded appearance --------------------------------------------------------


def test_an_appearance_is_checked_against_its_hash_and_its_shape() -> None:
    good = _appearance()
    with pytest.raises(ValueError, match="does not match its sha256"):
        Appearance.model_validate(good.model_dump() | {"sha256": "0" * 64})
    with pytest.raises(ValueError, match="not a PNG"):
        Appearance.of(b"GIF89a....", w=1, h=1)
    huge = _png(Image.effect_noise((200, 200), 100).convert("RGB"))
    with pytest.raises(ValueError, match="a control's crop is under"):
        Appearance.of(huge, w=200, h=200)


# -- the validator -------------------------------------------------------------------


def _claim(**changes: Any) -> StepClaim:
    base: dict[str, Any] = {
        "step_id": "search.submit",
        "action": "click",
        "risk": "safe",
        "approval": "none",
        "target": [RoleName(role="button", name="Go", within=Within(frame=""))],
        "appearance": _appearance(),
    }
    return StepClaim(**(base | changes))


def _judge(
    matches: list[VisualMatch],
    *,
    claim: StepClaim | None = None,
    screen: Observation | None = None,
    masks: list[Rect] | None = None,
    policy: Policy = POLICY,
) -> Accepted | Refused:
    return validate(
        matches,
        claim=claim or _claim(),
        screen=screen or _observation(),
        screenshot_png=_screen((60, 30)),
        masks=masks or [],
        policy=policy,
        evidence_ref="screenshots/0001.png",
    )


ONE = [VisualMatch(bounds=BUTTON, confidence=1.0)]


def test_one_clear_match_on_a_safe_click_becomes_a_guarded_candidate() -> None:
    verdict = _judge([*ONE, VisualMatch(bounds=Rect(x=0, y=0, w=40, h=16), confidence=0.6)])
    assert isinstance(verdict, Accepted), verdict
    c = verdict.candidate
    assert (c.action.x, c.action.y) == BUTTON.center
    assert c.action.guard == BUTTON
    assert c.action.guard_sha256 == image.pixels_sha256(_screen((60, 30)), BUTTON)
    assert (c.action.role, c.action.name) == ("button", "Go")
    assert c.evidence_ref == "screenshots/0001.png" and c.confidence == 1.0


@pytest.mark.parametrize(
    ("claim", "why"),
    [
        (_claim(action="type"), "unsupported"),
        (_claim(risk="irreversible", approval="required"), "unsafe"),
        (_claim(approval="required"), "unsafe"),
        (_claim(appearance=_appearance(scale=2.0)), "scale"),
    ],
    ids=["type", "irreversible", "approval", "scale"],
)
def test_what_the_step_is_can_rule_vision_out(claim: StepClaim, why: str) -> None:
    verdict = _judge(ONE, claim=claim)
    assert isinstance(verdict, Refused) and verdict.why == why


def test_a_weak_match_is_not_found() -> None:
    verdict = _judge([VisualMatch(bounds=BUTTON, confidence=0.9)])
    assert isinstance(verdict, Refused) and verdict.why == "not_found"
    assert "0.900" in verdict.reason


def test_a_near_rival_makes_it_ambiguous_even_below_the_threshold() -> None:
    rival = VisualMatch(bounds=Rect(x=0, y=0, w=40, h=16), confidence=0.93)
    verdict = _judge([*ONE, rival])
    assert isinstance(verdict, Refused) and verdict.why == "ambiguous"


def test_a_match_off_the_screen_or_on_a_mask_is_refused() -> None:
    off = [VisualMatch(bounds=Rect(x=180, y=30, w=40, h=16), confidence=1.0)]
    verdict = _judge(off)
    assert isinstance(verdict, Refused) and verdict.why == "outside"
    verdict = _judge(ONE, masks=[Rect(x=90, y=40, w=50, h=20)])
    assert isinstance(verdict, Refused) and verdict.why == "masked"


def test_the_tree_is_not_overruled_by_a_picture() -> None:
    other = Node(ref="n1", role="button", name="Delete", bbox=BUTTON)
    verdict = _judge(ONE, screen=_observation(nodes=[other]))
    assert isinstance(verdict, Refused) and verdict.why == "contradicted"
    assert 'button "Delete"' in verdict.reason
    # A label under the point is not a control, and does not contradict.
    text = Node(ref="n2", role="text", name="Go", bbox=BUTTON)
    assert isinstance(_judge(ONE, screen=_observation(nodes=[text])), Accepted)


def test_a_screen_a_commit_rule_covers_is_refused_whatever_the_step_says() -> None:
    verdict = _judge(ONE, screen=_observation(REVIEW))
    assert isinstance(verdict, Refused) and verdict.why == "policy"
    assert "never made where a commit rule does" in verdict.reason


def test_a_policy_block_is_a_refusal() -> None:
    elsewhere = _observation("http://evil.example/search")
    verdict = _judge(ONE, screen=elsewhere)
    assert isinstance(verdict, Refused) and verdict.why == "policy"
    assert "outside the allowed origins" in verdict.reason


# -- the policy's reading of a click by pixels -----------------------------------------


def _point(name: str = "Search", url: str = SEARCH) -> tuple[ClickPoint, Observation]:
    point = ClickPoint(x=1, y=1, guard=BUTTON, guard_sha256="0" * 64, role="button", name=name)
    return point, _observation(url)


def test_a_click_by_pixels_is_judged_as_the_control_it_claims() -> None:
    point, screen = _point()
    assert check(POLICY, point, screen) == Allow()
    point, screen = _point("Confirm", REVIEW)
    assert isinstance(check(POLICY, point, screen), NeedsApproval)
    # Anywhere on a commit screen, whatever it claims to be.
    point, screen = _point("Back", REVIEW)
    decision = check(POLICY, point, screen)
    assert isinstance(decision, NeedsApproval) and "click by pixels" in decision.reason
    no_clicks = POLICY.model_copy(update={"allowed_actions": ["type"]})
    point, screen = _point()
    assert isinstance(check(no_clicks, point, screen), Block)


def test_vision_is_off_unless_a_policy_turns_it_on() -> None:
    bare = Policy(allowed_origins=["http://127.0.0.1:8000"])
    assert bare.vision.allowed is False
    assert POLICY.vision.allowed is True


# -- what it asks of a surface -----------------------------------------------------------


def _cap(name: str) -> Capability:
    data = json.loads((REPO_ROOT / "capabilities" / name).read_text(encoding="utf-8"))
    return Capability.model_validate(data)


def test_a_vision_run_needs_screenshots_and_a_pointer() -> None:
    web = _cap("member_savings_balance.json")
    assert "pointer" in PLAYWRIGHT.features and "pointer" not in WINDOWS_UIA.features
    wanted = requirements.for_run(web, handoff=False, vision=True)
    assert {"screenshots", "pointer"} <= set(wanted.run)
    assert requirements.check(web, PLAYWRIGHT, handoff=False, vision=True).ok
    desk = _cap("deskcalc_compute.json")
    fits = requirements.check(desk, WINDOWS_UIA, handoff=False, vision=True)
    assert fits.missing == ["screenshots", "pointer"]
    assert requirements.check(desk, WINDOWS_UIA, handoff=False).ok


# -- schema 1.3 ------------------------------------------------------------------------


def _step(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "search.submit",
        "action": "click",
        "target": [{"strategy": "role_name", "role": "button", "name": "Search"}],
    }
    return base | changes


def test_only_a_click_step_has_an_appearance() -> None:
    picture = _appearance().model_dump()
    assert Step.model_validate(_step(appearance=picture)).appearance is not None
    with pytest.raises(ValueError, match="only a click step has an appearance"):
        Step.model_validate(_step(action="type", value="x", appearance=picture))


def test_an_appearance_is_a_schema_1_3_field_and_older_hashes_stand() -> None:
    for name in ("member_savings_balance.json", "open_subaccount.json", "deskcalc_compute.json"):
        data = json.loads((REPO_ROOT / "capabilities" / name).read_text(encoding="utf-8"))
        assert Capability.model_validate(data).content_hash() == data["content_sha256"]
    data = json.loads(
        (REPO_ROOT / "capabilities" / "deskcalc_compute.json").read_text(encoding="utf-8")
    )
    assert data["schema_version"] == "1.2"
    click = next(i for i, s in enumerate(data["steps"]) if s["action"] == "click")
    data["steps"][click]["appearance"] = _appearance().model_dump()
    with pytest.raises(ValueError, match=r"appearance is a schema 1.3 field"):
        Capability.model_validate(data)
    data["schema_version"] = "1.3"
    with_picture = Capability.model_validate(data)
    data["steps"][click].pop("appearance")
    assert with_picture.content_hash() != Capability.model_validate(data).content_hash()


def test_the_exported_json_schema_is_the_1_3_one() -> None:
    from cua.artifact.schema import SCHEMA_PATH, json_schema

    assert SCHEMA_PATH.name == "capability-1.3.json"
    on_disk = json.loads((REPO_ROOT / SCHEMA_PATH).read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(json_schema()))


# -- the recorder ------------------------------------------------------------------------

DISCOVERY = REPO_ROOT / "evidence" / "discovery-member-savings-balance"


def test_the_recorder_crops_each_clicked_control_from_its_screen() -> None:
    cap = record(DISCOVERY, policy=POLICY)
    pictures = {s.id: s.appearance for s in cap.steps if s.appearance is not None}
    assert sorted(pictures) == ["login.submit", "search.submit"]
    search = pictures["search.submit"]
    assert (search.w, search.h, search.scale) == (63, 20, 1.0)
    png = base64.b64decode(search.png_base64)
    # The picture is the control: it is found on the screen it was cut from.
    shots = sorted((DISCOVERY / "screenshots").glob("*.png"))
    best = max(
        (m.confidence for s in shots for m in TemplateDetector().detect(s.read_bytes(), search)),
        default=0.0,
    )
    assert best == pytest.approx(1.0) and png.startswith(b"\x89PNG")


def test_a_control_under_a_mask_gets_no_appearance() -> None:
    masked = POLICY.model_copy(
        update={
            "screenshot_masks": [
                *POLICY.screenshot_masks,
                [NearText(text="Member ID", role="button")],
            ]
        }
    )
    cap = record(DISCOVERY, policy=masked)
    by_id = {s.id: s for s in cap.steps}
    assert by_id["search.submit"].appearance is None
    assert by_id["login.submit"].appearance is not None


# -- drift ---------------------------------------------------------------------------------


def test_a_control_only_vision_found_is_control_unlabeled(tmp_path: Path) -> None:
    d = built_run(
        tmp_path,
        [
            step("search.submit", 1),
            resolved(
                "search.submit",
                2,
                "vision",
                "role_name",
                [
                    {"rung": "role_name", "matches": 0},
                    {"rung": "bbox", "matches": 0},
                    {"rung": "vision", "matches": 1},
                ],
            ),
        ],
    )
    drift = read_drift(d)
    assert drift is not None
    [event] = drift.events
    assert (event.kind, event.fatal, event.resolved_rung) == ("CONTROL_UNLABELED", False, "vision")
    assert "vision found its recorded picture" in event.reason
