"""One contract suite, inherited by the browser and live Windows adapters.

The adapter fixture owns the target and runs checks on its automation thread.
Checks use Surface's observations/actions, never DOM or UIA handles.
"""

from collections.abc import Callable
from dataclasses import dataclass

import httpx
import pytest

from cua.surface.conditions import (
    AllOf,
    AnyOf,
    DialogRaised,
    ErrorBannerPresent,
    LocationMatches,
    OutputExtracted,
    RegionPresent,
    TextPresent,
    ValueSet,
    Visible,
)
from cua.surface.features import FEATURES, SurfaceDescriptor
from cua.surface.locators import (
    Ambiguous,
    BBox,
    Resolved,
    RoleName,
    Unresolved,
    Within,
    resolve_ladder,
)
from cua.surface.protocol import (
    Click,
    ClickPoint,
    ConditionTimeout,
    ExpectDialog,
    Navigate,
    PerceptionDrift,
    Press,
    ReadText,
    RecordingEnv,
    Rect,
    SelectOption,
    StaleRefError,
    Surface,
    SurfaceError,
    TypeText,
    Viewport,
)
from cua.surface.vision.image import pixel_box, pixels_sha256

FEATURE_CONTRACTS = {
    "accessibility_tree": "test_refs_expire_and_never_address_a_replacement",
    "geometry": "test_descriptor_matches_registered_features",
    "fixed_viewport": "test_pixel_rung_refuses_a_different_viewport",
    "frames": "test_location_and_frame_scopes_do_not_widen",
    "locations": "test_location_and_frame_scopes_do_not_widen",
    "document_status": "test_document_status_detects_a_real_error_response",
    "forms": "test_forms_round_trip_typed_values",
    "keyboard": "test_keyboard_changes_only_the_named_field",
    "dialogs": "test_native_dialogs_are_answered_and_recorded",
    "screenshots": "test_screenshots_are_masked_before_capture",
    "pointer": "test_pointer_refuses_changed_pixels",
    "egress_control": "test_egress_control_refuses_another_origin",
    "session_handoff": "test_handoff_publishes_the_owned_live_target",
}


@dataclass
class SurfaceCase:
    surface: Surface
    descriptor: SurfaceDescriptor
    entry: str
    closed: Callable[[], bool]
    mutate_field: Callable[[str], None] | None = None

    def ref(self, role, name):
        answer = self.surface.resolve([RoleName(role=role, name=name)])
        assert isinstance(answer, Resolved), answer
        return answer.ref


class SurfaceContract:
    """Implement a `contract(check, inject='')` fixture to reuse these tests.

    The fixture must assert its independent lifecycle oracle in a finally block,
    after the surface's context exits, including when the check raises.
    """

    def test_descriptor_matches_registered_features(self, contract):
        def check(case):
            assert set(FEATURE_CONTRACTS) == set(FEATURES), (
                "new features need an explicit contract probe"
            )
            assert case.surface.descriptor == case.descriptor
            assert case.descriptor.features <= set(FEATURES)
            obs = case.surface.observe()
            assert obs.nodes and len({n.ref for n in obs.nodes}) == len(obs.nodes)
            assert obs.viewport.w > 0 and obs.viewport.h > 0
            controls = [n for n in obs.nodes if n.role in ("textbox", "button", "combobox")]
            assert controls
            if "geometry" in case.descriptor.features:
                assert all(n.bbox and not n.bbox.empty for n in controls)

        contract(check)

    def test_refs_expire_and_never_address_a_replacement(self, contract):
        def check(case):
            old = case.ref("button", "Calculate")
            first = case.surface.observe()
            second = case.surface.observe()
            assert {n.ref for n in first.nodes}.isdisjoint(n.ref for n in second.nodes)
            for action in (Click(ref=old), ReadText(ref=old), TypeText(ref=old, text="x")):
                with pytest.raises(StaleRefError):
                    case.surface.act(action)
            assert not case.surface.evaluate(TextPresent(text="4"))

        contract(check)

    def test_ambiguity_requires_a_narrower_scope(self, contract):
        def check(case):
            ambiguous = case.surface.resolve([RoleName(role="button", name="Calculate")])
            assert isinstance(ambiguous, Ambiguous) and ambiguous.matches == 2
            obs = case.surface.observe()
            legacy = next(n for n in obs.nodes if n.role == "group" and n.name == "Legacy")
            assert case.surface.evaluator.evaluate(RegionPresent(name="Legacy"), obs)
            button = next(n for n in obs.descendants(legacy.ref) if n.role == "button")
            assert button.bbox is not None
            # Within's closed vocabulary scopes frames. A measured fallback,
            # bound to the recording viewport, narrows this same-frame duplicate.
            scoped = case.surface.resolve(
                [
                    RoleName(role="button", name="Calculate"),
                    BBox(**button.bbox.model_dump(), role="button"),
                ],
                observation=obs,
                recording_env=RecordingEnv(viewport=obs.viewport),
            )
            assert isinstance(scoped, Resolved)
            assert scoped.slipped and scoped.ref == button.ref
            absent = case.surface.resolve(
                [RoleName(role="button", name="Calculate", within=Within(frame="missing"))]
            )
            assert isinstance(absent, Unresolved)

        contract(check, inject="ambiguous")

    def test_pixel_rung_refuses_a_different_viewport(self, contract):
        def check(case):
            if not {"geometry", "fixed_viewport"} <= case.descriptor.features:
                pytest.skip("adapter does not claim fixed geometry")
            observation = case.surface.observe()
            node = observation.node(case.ref("button", "Calculate"))
            assert node.bbox is not None
            rung = BBox(**node.bbox.model_dump(), role="button")
            wrong = RecordingEnv(
                viewport=Viewport(w=observation.viewport.w + 100, h=observation.viewport.h)
            )
            answer = resolve_ladder([rung], observation, recording_env=wrong)
            assert isinstance(answer, Unresolved)
            assert "differs" in answer.attempts[0].refused
            right = RecordingEnv(viewport=observation.viewport)
            assert isinstance(resolve_ladder([rung], observation, recording_env=right), Resolved)

        contract(check)

    def test_conditions_read_one_observation_and_waits_report_the_last_screen(self, contract):
        def check(case):
            obs = case.surface.observe()
            target = next(
                n.ref for n in obs.nodes if n.role == "textbox" and n.name == "First number"
            )
            evaluator = case.surface.evaluator
            assert evaluator.evaluate(Visible(), obs, target=target)
            assert evaluator.evaluate(TextPresent(text="First number"), obs)
            assert not evaluator.evaluate(ValueSet(value="2"), obs, target=target)
            condition = AllOf(
                all_of=[TextPresent(text="First number"), AnyOf(any_of=[OutputExtracted(name="x")])]
            )
            assert evaluator.evaluate(condition, obs, outputs={"x": 0})
            assert not evaluator.evaluate(condition, obs, outputs={})
            with pytest.raises(ConditionTimeout) as error:
                case.surface.wait_for(TextPresent(text="missing-contract-sentinel"), 0.1)
            assert error.value.observation.nodes
            assert error.value.condition.text == "missing-contract-sentinel"

        contract(check)

    def test_forms_round_trip_typed_values(self, contract):
        def check(case):
            if "forms" not in case.descriptor.features:
                pytest.skip("adapter does not claim forms")
            case.surface.act(TypeText(ref=case.ref("textbox", "First number"), text="2"))
            case.surface.act(TypeText(ref=case.ref("textbox", "Second number"), text="2"))
            case.surface.act(SelectOption(ref=case.ref("combobox", "Operation"), value="Multiply"))
            case.surface.act(Click(ref=case.ref("button", "Calculate")))
            case.surface.wait_for(TextPresent(text="4"), 5)
            answer = case.surface.act(ReadText(ref=case.ref("textbox", "Result")))
            assert answer.text == "4"

        contract(check)

    def test_keyboard_changes_only_the_named_field(self, contract):
        def check(case):
            if "keyboard" not in case.descriptor.features:
                pytest.skip("adapter does not claim keyboard")
            target = case.ref("textbox", "First number")
            case.surface.act(TypeText(ref=target, text="12"))
            case.surface.act(Press(ref=case.ref("textbox", "First number"), key="End"))
            case.surface.act(Press(ref=case.ref("textbox", "First number"), key="Backspace"))
            assert case.surface.act(ReadText(ref=case.ref("textbox", "First number"))).text == "1"
            assert case.surface.act(ReadText(ref=case.ref("textbox", "Second number"))).text == ""

        contract(check)

    @pytest.mark.parametrize("declared", [False, True])
    def test_native_dialogs_are_answered_and_recorded(self, contract, declared):
        def check(case):
            if "dialogs" not in case.descriptor.features:
                pytest.skip("adapter does not claim dialogs")
            if declared:
                case.surface.expect_dialog(
                    ExpectDialog(accept=True, message_contains="rate service")
                )
            answer = case.surface.act(Click(ref=case.ref("button", "Calculate")))
            (dialog,) = answer.dialogs
            assert (dialog.answer, dialog.expected) == (
                "accepted" if declared else "dismissed",
                declared,
            )
            obs = case.surface.observe()
            assert case.surface.evaluator.evaluate(DialogRaised(text="rate service"), obs)
            assert not case.surface.evaluator.evaluate(TextPresent(text="rate service"), obs)

        contract(check, inject="modal")

    def test_location_and_frame_scopes_do_not_widen(self, contract):
        def check(case):
            if "locations" not in case.descriptor.features:
                pytest.skip("adapter does not claim locations")
            obs = case.surface.observe()
            assert case.surface.evaluator.evaluate(LocationMatches(pattern="(DeskCalc|panel)"), obs)
            assert not case.surface.evaluator.evaluate(
                LocationMatches(pattern=".*", within=Within(frame="missing")), obs
            )
            if "frames" in case.descriptor.features:
                assert {f.name for f in obs.frames} >= {"main", "nav"}
                assert isinstance(
                    case.surface.resolve(
                        [RoleName(role="button", name="Calculate", within=Within(frame="main"))]
                    ),
                    Resolved,
                )
                assert not case.surface.evaluator.evaluate(
                    TextPresent(text="First number", within=Within(frame="nav")), obs
                )

        contract(check, inject="frames")

    def test_screenshots_are_masked_before_capture(self, contract):
        def check(case):
            if "screenshots" not in case.descriptor.features:
                with pytest.raises(SurfaceError):
                    case.surface.observe(screenshot=True)
                return
            import io

            from PIL import Image

            case.surface.act(
                TypeText(ref=case.ref("textbox", "First number"), text="synthetic-secret")
            )
            unmasked = case.surface.observe(screenshot=True)
            visible = next(
                n for n in unmasked.nodes if n.role == "textbox" and n.name == "First number"
            )
            assert unmasked.screenshot_png and visible.bbox
            with Image.open(io.BytesIO(unmasked.screenshot_png)) as picture:
                box = visible.bbox
                crop = picture.crop((int(box.x), int(box.y), int(box.right), int(box.bottom)))
                assert crop.convert("RGB").getcolors(maxcolors=1) is None
            obs = case.surface.observe(
                screenshot=True, masks=[[RoleName(role="textbox", name="First number")]]
            )
            node = next(n for n in obs.nodes if n.role == "textbox" and n.name == "First number")
            assert obs.screenshot_png and node.bbox
            with Image.open(io.BytesIO(obs.screenshot_png)) as picture:
                assert picture.convert("RGB").getpixel(tuple(map(int, node.bbox.center))) == (
                    16,
                    16,
                    16,
                )

        contract(check)

    def test_pointer_refuses_changed_pixels(self, contract):
        def check(case):
            if "pointer" not in case.descriptor.features:
                pytest.skip("adapter does not claim guarded pointer input")
            case.surface.act(TypeText(ref=case.ref("textbox", "First number"), text="2"))
            case.surface.act(TypeText(ref=case.ref("textbox", "Second number"), text="2"))
            case.surface.act(SelectOption(ref=case.ref("combobox", "Operation"), value="Multiply"))
            ready = case.surface.observe(screenshot=True)
            button = next(n for n in ready.nodes if n.role == "button" and n.name == "Calculate")
            assert ready.screenshot_png and button.bbox
            x, y = button.bbox.center
            # Guard the same whole pixels the vision pipeline records. Browser
            # layout boxes can be fractional; a fractional clip is a different
            # raster from cropping the original screenshot at pixel boundaries.
            left, top, right, bottom = pixel_box(
                button.bbox, bounds=(ready.viewport.w, ready.viewport.h)
            )
            guard = Rect(x=left, y=top, w=right - left, h=bottom - top)
            case.surface.act(
                ClickPoint(
                    x=x,
                    y=y,
                    guard=guard,
                    guard_sha256=pixels_sha256(ready.screenshot_png, guard),
                    role="button",
                    name="Calculate",
                )
            )
            case.surface.wait_for(TextPresent(text="4"), 5)
            assert case.surface.act(ReadText(ref=case.ref("textbox", "Result"))).text == "4"
            obs = case.surface.observe(screenshot=True)
            node = next(n for n in obs.nodes if n.role == "textbox" and n.name == "First number")
            assert obs.screenshot_png and node.bbox
            left, top, right, bottom = pixel_box(node.bbox, bounds=(obs.viewport.w, obs.viewport.h))
            guard = Rect(x=left, y=top, w=right - left, h=bottom - top)
            digest = pixels_sha256(obs.screenshot_png, guard)
            assert case.mutate_field is not None, "claimed pointer needs an external mutation probe"
            case.mutate_field("changed picture")
            x, y = node.bbox.center
            with pytest.raises(PerceptionDrift):
                case.surface.act(ClickPoint(x=x, y=y, guard=guard, guard_sha256=digest))

        contract(check)

    def test_handoff_publishes_the_owned_live_target(self, contract):
        def check(case):
            if "session_handoff" not in case.descriptor.features:
                with pytest.raises(SurfaceError):
                    case.surface.expose()
                return
            handle = case.surface.expose()
            response = httpx.get(handle.cdp_url + "/json/list", timeout=5)
            response.raise_for_status()
            assert any(
                t["id"] == handle.target_id and t["url"] == handle.page_url for t in response.json()
            )

        contract(check)

    def test_document_status_detects_a_real_error_response(self, contract):
        def check(case):
            if "document_status" not in case.descriptor.features:
                pytest.skip("adapter does not claim document status")
            obs = case.surface.observe()
            assert any(f.status == 500 for f in obs.frames)
            assert case.surface.evaluator.evaluate(ErrorBannerPresent(), obs)

        contract(check, inject="error")

    def test_egress_control_refuses_another_origin(self, contract):
        def check(case):
            if "egress_control" not in case.descriptor.features:
                pytest.skip("adapter does not claim egress control")
            from urllib.parse import urlsplit

            origin = urlsplit(case.entry)
            case.surface.restrict_egress([f"{origin.scheme}://{origin.netloc}"])
            with pytest.raises(SurfaceError):
                case.surface.act(Navigate(url=case.entry.replace("127.0.0.1", "localhost")))
            assert case.surface.egress_blocked

        contract(check)

    def test_lifecycle_cleanup_also_runs_after_a_failed_check(self, contract):
        class CheckFailed(Exception):
            pass

        def check(case):
            assert not case.closed()
            raise CheckFailed

        with pytest.raises(CheckFailed):
            contract(check)
