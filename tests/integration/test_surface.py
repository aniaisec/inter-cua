"""The Surface against the real mock app in a real browser.

The unit tests prove the rungs are correct about a tree; these prove the tree
is correct about the app. Everything here drives the app the way a recorded
capability will: navigate, resolve a ladder, act on the ref it returns. No CSS
selector appears in this file, because nothing in ``src/cua`` may use one and a
test that cheated would not be testing the thing that ships.
"""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import Browser, Page

from cua.surface import playwright_surface
from cua.surface.conditions import DialogRaised, RegionPresent, TextPresent, ValueSet
from cua.surface.evaluators import WebEvaluator
from cua.surface.locators import BBox, NearText, Resolved, RoleName, TableCell, Unresolved, Within
from cua.surface.playwright_surface import PlaywrightSurface
from cua.surface.protocol import (
    ActionFailed,
    Click,
    ConditionTimeout,
    ExpectDialog,
    Navigate,
    PerceptionDrift,
    ReadText,
    RecordingEnv,
    StaleRefError,
    SurfaceError,
    TypeText,
    Viewport,
)
from mockapp import injects
from mockapp.data import MEMBERS, format_currency
from mockapp.injects import Inject

pytestmark = pytest.mark.browser

USER_ID = [NearText(text="User ID", role="textbox")]
PASSWORD = [NearText(text="Password", role="textbox")]
SIGN_ON = [RoleName(role="button", name="Sign On")]
MEMBER_ID = [NearText(text="Member ID", role="textbox")]
SEARCH_SUBMIT = [
    RoleName(role="button", name="Search"),
    NearText(text="Member ID", role="button"),
]
SAVINGS_BALANCE = [TableCell(row_contains="Savings", column_header="Balance")]


@pytest.fixture
def surface(page: Page):
    return PlaywrightSurface(page)


def click(surface: PlaywrightSurface, ladder) -> None:
    surface.act(Click(ref=_resolved(surface, ladder).ref))


def fill(surface: PlaywrightSurface, ladder, text: str) -> None:
    surface.act(TypeText(ref=_resolved(surface, ladder).ref, text=text))


def _resolved(surface: PlaywrightSurface, ladder) -> Resolved:
    outcome = surface.resolve(ladder)
    assert isinstance(outcome, Resolved), f"{ladder} -> {outcome}"
    return outcome


def sign_on(surface: PlaywrightSurface, base_url: str, *, inject: Inject | None = None) -> None:
    query = f"?inject={inject.value}" if inject else ""
    surface.act(Navigate(url=f"{base_url}/login{query}"))
    fill(surface, USER_ID, "operator")
    fill(surface, PASSWORD, "operator")
    click(surface, SIGN_ON)
    surface.wait_for(RegionPresent(name="Member Search"), 10.0)


def open_member(surface: PlaywrightSurface, member_id: str, timeout_s: float = 10.0) -> None:
    fill(surface, MEMBER_ID, member_id)
    click(surface, SEARCH_SUBMIT)
    surface.wait_for(RegionPresent(name="Balances"), timeout_s)


# -- perception ------------------------------------------------------------


def test_perception_walks_every_frame_of_the_frameset(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    sign_on(surface, mockapp_url)
    observation = surface.observe()

    assert {n.frame for n in observation.nodes} == {"nav", "main"}
    assert any(n.role == "link" and n.name == "Member Search" for n in observation.nodes)
    assert any(n.role == "button" and n.name == "Search" for n in observation.nodes)


def test_the_search_box_has_no_name_and_is_described_by_the_label_beside_it(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    """The measured-geometry version of the contract the unit tests assume."""
    sign_on(surface, mockapp_url)
    observation = surface.observe()

    textboxes = [n for n in observation.nodes if n.role == "textbox"]
    assert [t.name for t in textboxes] == [""]
    assert textboxes[0].near_text == "Member ID"
    assert textboxes[0].bbox is not None


def test_the_compact_tree_is_what_a_decision_can_be_made_from(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    sign_on(surface, mockapp_url)
    compact = surface.observe().compact()

    assert "[nav]" in compact and "[main]" in compact
    assert 'link "Member Search"' in compact
    assert "textbox ~'Member ID'" in compact
    assert 'button "Search"' in compact
    assert "rowgroup" not in compact


def test_the_frameset_shell_itself_contributes_no_nodes(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    """A ``<frameset>`` document has no body to snapshot, and that is not an error."""
    sign_on(surface, mockapp_url)
    observation = surface.observe()

    assert observation.location.rstrip("/") == mockapp_url
    assert [f.name for f in observation.frames] == ["", "nav", "main"]
    assert not observation.in_frame("")


# -- acting ----------------------------------------------------------------


def test_the_happy_path_walks_on_ladders_alone(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    sign_on(surface, mockapp_url)
    open_member(surface, "10003")

    balance = _resolved(surface, SAVINGS_BALANCE)
    result = surface.act(ReadText(ref=balance.ref))

    assert result.text == format_currency(MEMBERS["10003"].savings)
    assert balance.rung == "table_cell"


def test_typing_into_an_unnamed_field_is_confirmed_by_reading_it_back(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    sign_on(surface, mockapp_url)
    fill(surface, MEMBER_ID, "10003")

    found = _resolved(surface, MEMBER_ID)
    assert surface.evaluate(ValueSet(value="10003"), target=found.ref)


def test_a_ref_from_an_earlier_screen_addresses_nothing(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    """The failure mode a stale element handle hides: the page moved on.

    Every observation mints new refs, so the search button's ref does not come
    back to life as some control on the member detail screen.
    """
    sign_on(surface, mockapp_url)
    submit = _resolved(surface, SEARCH_SUBMIT)

    main = surface.page.frame(name="main")
    assert main is not None
    main.goto(f"{mockapp_url}/member/10003")
    surface.wait_for(RegionPresent(name="Balances"), 10.0)

    with pytest.raises(StaleRefError):
        surface.act(Click(ref=submit.ref))


def test_a_screen_that_changes_between_looking_and_acting_is_a_fault(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    """The narrow window a ref cannot protect itself from, closed by re-checking.

    Nothing is re-observed here, so the ref is still current — but the node it
    was minted for is gone. Acting anyway would click whatever now sits in that
    position in the tree.
    """
    sign_on(surface, mockapp_url)
    submit = _resolved(surface, SEARCH_SUBMIT)

    main = surface.page.frame(name="main")
    assert main is not None
    main.goto(f"{mockapp_url}/member/10003")
    main.wait_for_load_state("load")

    with pytest.raises(PerceptionDrift):
        surface.act(Click(ref=submit.ref))


def test_a_ref_cannot_outlive_the_action_that_changed_the_screen(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    sign_on(surface, mockapp_url)
    submit = _resolved(surface, SEARCH_SUBMIT)
    surface.act(Click(ref=submit.ref))

    with pytest.raises(StaleRefError):
        surface.act(Click(ref=submit.ref))


# -- waiting ---------------------------------------------------------------


def test_a_slow_screen_is_waited_for_not_failed(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    """``slow_load``: four seconds is slow, not broken."""
    sign_on(surface, mockapp_url, inject=Inject.SLOW_LOAD)
    open_member(surface, "10003", timeout_s=15.0)

    assert surface.evaluate(TextPresent(text=format_currency(MEMBERS["10003"].savings)))


def test_a_condition_that_never_holds_times_out_with_the_evidence(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    sign_on(surface, mockapp_url)

    with pytest.raises(ConditionTimeout) as caught:
        surface.wait_for(RegionPresent(name="Review Sub-account"), 1.0)

    assert caught.value.observation.nodes, "a timeout must carry what was on screen"
    assert "Member Search" in caught.value.observation.compact()


# -- drift -----------------------------------------------------------------


def test_a_renamed_button_falls_through_to_the_label_beside_it(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    """``renamed_button``: "Search" now reads "Find"."""
    sign_on(surface, mockapp_url, inject=Inject.RENAMED_BUTTON)

    strong = surface.resolve([RoleName(role="button", name="Search")])
    assert isinstance(strong, Unresolved)

    outcome = _resolved(surface, SEARCH_SUBMIT)
    assert outcome.rung == "near_text"
    assert outcome.slipped
    assert outcome.node.name == "Find"


def test_two_search_buttons_make_the_strong_rung_fall_through(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    """``ambiguous_button``: the nav frame grows its own "Search".

    The rung that matched one node when this capability was recorded now
    matches two, in different frames. It must decline rather than pick.
    """
    sign_on(surface, mockapp_url, inject=Inject.AMBIGUOUS_BUTTON)

    outcome = _resolved(surface, SEARCH_SUBMIT)
    assert outcome.attempts[0].rung == "role_name"
    assert outcome.attempts[0].matches == 2
    assert outcome.rung == "near_text"
    assert outcome.node.frame == "main"

    # And the run still works, on the rung that could tell them apart.
    surface.act(Click(ref=outcome.ref))
    surface.wait_for(TextPresent(text="No matching member"), 10.0)


def test_a_frame_scoped_rung_is_not_ambiguous_to_begin_with(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    sign_on(surface, mockapp_url, inject=Inject.AMBIGUOUS_BUTTON)
    outcome = _resolved(
        surface, [RoleName(role="button", name="Search", within=Within(frame="main"))]
    )
    assert outcome.rung == "role_name"
    assert outcome.node.frame == "main"


# -- pixels ----------------------------------------------------------------


def test_a_pixel_rung_works_in_the_window_it_was_recorded_in(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    sign_on(surface, mockapp_url)
    observation = surface.observe()
    textbox = next(n for n in observation.nodes if n.role == "textbox")
    assert textbox.bbox is not None

    outcome = surface.resolve(
        [BBox(x=textbox.bbox.x, y=textbox.bbox.y, w=textbox.bbox.w, h=textbox.bbox.h)],
        observation=observation,
        recording_env=RecordingEnv(viewport=observation.viewport),
    )
    assert isinstance(outcome, Resolved)
    assert outcome.node.ref == textbox.ref


def test_a_pixel_rung_is_refused_in_a_window_of_another_size(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    sign_on(surface, mockapp_url)
    recorded = surface.observe()
    textbox = next(n for n in recorded.nodes if n.role == "textbox")
    assert textbox.bbox is not None
    ladder = [BBox(x=textbox.bbox.x, y=textbox.bbox.y, w=textbox.bbox.w, h=textbox.bbox.h)]

    surface.page.set_viewport_size({"width": 800, "height": 600})
    resized = surface.observe()
    assert resized.viewport == Viewport(w=800, h=600)

    outcome = surface.resolve(
        ladder, observation=resized, recording_env=RecordingEnv(viewport=recorded.viewport)
    )
    assert isinstance(outcome, Unresolved)
    assert outcome.attempts[0].refused is not None


# -- redaction and handoff -------------------------------------------------


def test_a_masked_field_never_reaches_the_screenshot(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    """Masks are applied by the capture, not cleaned up after it."""
    surface.act(Navigate(url=f"{mockapp_url}/login"))
    fill(surface, PASSWORD, "operator")

    plain = surface.observe(screenshot=True).screenshot_png
    masked = surface.observe(screenshot=True, masks=[PASSWORD]).screenshot_png

    assert plain is not None and masked is not None
    assert plain.startswith(b"\x89PNG") and masked.startswith(b"\x89PNG")
    assert plain != masked


def test_a_live_session_can_be_published_for_a_human_to_take_over(
    page: Page, mockapp_url: str, browser_session, operator_browser: Browser
) -> None:
    attachable = PlaywrightSurface(page, cdp_url=browser_session.cdp_url)
    sign_on(attachable, mockapp_url)

    handle = attachable.expose()
    assert handle.cdp_url == browser_session.cdp_url
    assert any(p.url == handle.page_url for ctx in operator_browser.contexts for p in ctx.pages), (
        "the operator's browser sees the page the automation is on"
    )


def test_a_session_with_no_debugging_endpoint_says_so(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    with pytest.raises(SurfaceError):
        surface.expose()


# -- dialogs ---------------------------------------------------------------

OPEN_SUBACCOUNT = [RoleName(role="link", name="Open Sub-account")]
INITIAL_DEPOSIT = [NearText(text="Initial Deposit", role="textbox")]
CONTINUE = [RoleName(role="button", name="Continue")]
CONFIRM = [RoleName(role="button", name="Confirm")]


def reach_review(surface: PlaywrightSurface, base_url: str, *, inject: Inject) -> None:
    sign_on(surface, base_url, inject=inject)
    open_member(surface, "10003")
    click(surface, OPEN_SUBACCOUNT)
    surface.wait_for(RegionPresent(name="Open Sub-account"), 10.0)
    fill(surface, INITIAL_DEPOSIT, "25.00")
    click(surface, CONTINUE)
    surface.wait_for(RegionPresent(name="Review Sub-account"), 10.0)


def test_an_undeclared_confirm_is_dismissed_and_reported_not_silently_swallowed(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    """``native_confirm``: a dialog the capability never saw, on the irreversible step.

    It is outside the tree and the screenshot, and it blocks the browser while
    open, so the surface answers at once — conservatively — and says so. The
    point is the last assertion: nothing was committed, and the result does
    not pretend otherwise.
    """
    reach_review(surface, mockapp_url, inject=Inject.NATIVE_CONFIRM)

    result = surface.act(Click(ref=_resolved(surface, CONFIRM).ref))

    assert [(d.kind, d.answer, d.expected) for d in result.dialogs] == [
        ("confirm", "dismissed", False)
    ]
    assert "sub-account application" in result.dialogs[0].message

    after = surface.observe()
    assert WebEvaluator().evaluate(DialogRaised(expected=False), after)
    assert surface.evaluate(RegionPresent(name="Review Sub-account"))
    assert not surface.evaluate(TextPresent(text="Reference number"))


def test_a_declared_confirm_is_answered_as_the_capability_decided(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    reach_review(surface, mockapp_url, inject=Inject.NATIVE_CONFIRM)

    surface.expect_dialog(ExpectDialog(accept=True, message_contains="sub-account application"))
    result = surface.act(Click(ref=_resolved(surface, CONFIRM).ref))

    assert [(d.answer, d.expected) for d in result.dialogs] == [("accepted", True)]
    surface.wait_for(TextPresent(text="Reference number"), 10.0)


def test_a_declaration_for_a_different_dialog_does_not_answer_this_one(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    """An "accept" decided for one question is not an answer to another."""
    reach_review(surface, mockapp_url, inject=Inject.NATIVE_CONFIRM)

    surface.expect_dialog(ExpectDialog(accept=True, message_contains="wire transfer"))
    result = surface.act(Click(ref=_resolved(surface, CONFIRM).ref))

    assert [(d.answer, d.expected) for d in result.dialogs] == [("dismissed", False)]
    assert not surface.evaluate(TextPresent(text="Reference number"))


def test_a_declaration_does_not_outlive_its_action(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    """Declared for a step that raised nothing, it must not answer a later one."""
    reach_review(surface, mockapp_url, inject=Inject.NATIVE_CONFIRM)

    surface.expect_dialog(ExpectDialog(accept=True))
    surface.act(ReadText(ref=_resolved(surface, CONFIRM).ref))  # raises no dialog

    result = surface.act(Click(ref=_resolved(surface, CONFIRM).ref))
    assert [(d.answer, d.expected) for d in result.dialogs] == [("dismissed", False)]


def test_an_ordinary_click_reports_no_dialogs(surface: PlaywrightSurface, mockapp_url: str) -> None:
    sign_on(surface, mockapp_url)
    fill(surface, MEMBER_ID, "10003")
    result = surface.act(Click(ref=_resolved(surface, SEARCH_SUBMIT).ref))
    assert result.dialogs == []


def test_an_in_page_modal_is_seen_blocks_the_screen_and_can_be_dismissed(
    surface: PlaywrightSurface, mockapp_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``modal_dialog``: an overlay made of ordinary markup.

    Unlike a native dialog, perception sees it — as more page content, with
    no role to say it is modal. What makes it a dialog is that it covers the
    screen: a control resolved underneath it cannot be clicked, and that
    surfaces as a fault rather than a click that silently went nowhere.
    """
    monkeypatch.setattr(playwright_surface, "ACTION_TIMEOUT_MS", 1_500)
    sign_on(surface, mockapp_url, inject=Inject.MODAL_DIALOG)
    open_member(surface, "10003")

    assert surface.evaluate(TextPresent(text=injects.MODAL_TEXT))
    assert not surface.observe().dialogs, "an overlay is not a native dialog"

    with pytest.raises(ActionFailed):
        click(surface, OPEN_SUBACCOUNT)

    click(surface, [RoleName(role="button", name="OK")])
    click(surface, OPEN_SUBACCOUNT)
    surface.wait_for(RegionPresent(name="Open Sub-account"), 10.0)


def test_every_look_during_a_slow_navigation_is_consistent_and_quick(
    surface: PlaywrightSurface, mockapp_url: str
) -> None:
    """Regression: a read that straddles a navigation must not come back torn.

    The member detail page takes four seconds (``slow_load``), and the frame
    is observed in a tight loop across the moment it commits. Each look must
    describe one document — the search screen with all its boxes, or the
    detail screen — and none may stall. Before the fix, one look took the
    search tree, lost its geometry to the new document, and waited on every
    missing box in turn, outliving the whole ``wait_for``.
    """
    sign_on(surface, mockapp_url, inject=Inject.SLOW_LOAD)
    fill(surface, MEMBER_ID, "10003")
    click(surface, SEARCH_SUBMIT)

    looks = 0
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        started = time.monotonic()
        observation = surface.observe()
        took = time.monotonic() - started
        looks += 1

        assert took < 3.0, f"one observation took {took:.1f}s"
        main = [n for n in observation.nodes if n.frame == "main"]
        url = next(f.url for f in observation.frames if f.name == "main")
        if "/member/" in url:
            assert any(n.role == "heading" and n.name == "Balances" for n in main), url
            break
        controls = [n for n in main if n.interactive]
        assert all(n.bbox is not None for n in controls), "a torn read lost its geometry"
    else:
        pytest.fail("the member detail screen never arrived")

    assert looks > 3, "the loop should have straddled the navigation"


def test_a_timeout_says_what_it_saw(surface: PlaywrightSurface, mockapp_url: str) -> None:
    sign_on(surface, mockapp_url)
    with pytest.raises(ConditionTimeout, match=r"region 'Balances' present.*main=.*/search"):
        surface.wait_for(RegionPresent(name="Balances"), 0.5)
