"""Browser-level smoke tests.

Two jobs. First, prove the fixtures work: a real Chromium, a real frameset, a
real CDP endpoint a second party can attach to. Second, de-risk M1 on day one
as the plan asks, by asserting the things the Surface layer will depend on:
``aria_snapshot()`` works per frame, submit buttons expose their value as an
accessible name, and the text inputs expose no name at all.

Note on selectors: these tests use CSS to drive the app, which is fine — they
are the harness, not the system under test. Nothing under ``src/cua`` is
allowed to reach for a CSS or XPath selector.

Frames need waiting for. A frameset's child frames attach after the parent
document loads, so every helper here polls rather than asserting straight
after a click; M1's Surface will need the same discipline.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from playwright.sync_api import Browser, Frame, Page, expect
from playwright.sync_api import Error as PlaywrightError

from mockapp.data import MEMBERS, format_currency
from mockapp.injects import Inject

pytestmark = pytest.mark.browser

WAIT_S = 10.0


def wait_until(predicate: Callable[[], bool], what: str, timeout: float = WAIT_S) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def frame_named(page: Page, name: str) -> Frame:
    wait_until(
        lambda: (f := page.frame(name=name)) is not None and f.url.startswith("http"),
        f"frame {name!r} to attach and load",
    )
    frame = page.frame(name=name)
    assert frame is not None
    return frame


def settled_content(frame: Frame) -> str:
    """The frame's HTML once it has finished loading, or "" while a navigation
    is still under way — Playwright refuses to read a frame mid-navigation,
    and a click's navigation can start after the call that caused it returns."""
    try:
        frame.wait_for_load_state("load")
        return frame.content()
    except PlaywrightError as exc:
        if "navigating" in str(exc):
            return ""
        raise


def sign_in(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/login")
    page.fill("input[name=F_USRID]", "operator")
    page.fill("input[name=F_PWD]", "operator")
    page.click("input[type=submit]")
    page.wait_for_load_state("load")


def test_login_screen_renders_and_signs_in(page: Page, mockapp_url: str) -> None:
    sign_in(page, mockapp_url)
    expect(page).to_have_url(f"{mockapp_url}/")
    frame_named(page, "nav")
    frame_named(page, "main")
    assert {f.name for f in page.frames if f.name} == {"nav", "main"}


def test_aria_snapshot_works_per_frame(page: Page, mockapp_url: str) -> None:
    """page.accessibility.snapshot() is deprecated; aria_snapshot is the path."""
    sign_in(page, mockapp_url)

    nav_tree = frame_named(page, "nav").locator("body").aria_snapshot()
    assert "Member Search" in nav_tree

    main_tree = frame_named(page, "main").locator("body").aria_snapshot()
    assert 'heading "Member Search"' in main_tree
    assert 'button "Search"' in main_tree


def test_text_inputs_have_no_accessible_name(page: Page, mockapp_url: str) -> None:
    """The hostility contract that forces the near_text locator rung to exist."""
    sign_in(page, mockapp_url)
    tree = frame_named(page, "main").locator("body").aria_snapshot()
    assert "textbox" in tree
    assert 'textbox "Member ID"' not in tree


def test_happy_path_in_the_browser(page: Page, mockapp_url: str) -> None:
    sign_in(page, mockapp_url)
    frame = frame_named(page, "main")

    frame.fill("input[name=F_MBRID]", "10003")
    frame.click("input[type=submit]")
    savings = format_currency(MEMBERS["10003"].savings)
    wait_until(lambda: savings in settled_content(frame), "the member detail screen")

    frame.click("text=Open Sub-account")
    frame.wait_for_url("**/subaccount/10003")
    frame.fill("input[name=F_DEPAMT]", "25.00")
    frame.click("input[value=Continue]")
    frame.wait_for_url("**/review/10003")
    frame.click("input[value=Confirm]")
    wait_until(lambda: "Reference number" in settled_content(frame), "the confirmation screen")


def test_ambiguous_button_puts_a_second_search_button_in_the_nav_frame(
    page: Page, mockapp_url: str
) -> None:
    """The case the locator ladder must treat as a fault, not a coin flip."""
    page.goto(f"{mockapp_url}/login?inject={Inject.AMBIGUOUS_BUTTON.value}")
    sign_in(page, mockapp_url)
    frame_named(page, "nav")
    frame_named(page, "main")

    def both_frames_have_search() -> bool:
        hits = [f for f in page.frames if f.get_by_role("button", name="Search").count() > 0]
        return len(hits) == 2

    wait_until(both_frames_have_search, "one 'Search' button in each frame")


def test_operator_can_attach_to_the_live_session_over_cdp(
    page: Page, mockapp_url: str, operator_browser: Browser
) -> None:
    """Control transfer is only credible if a second party sees the same browser."""
    sign_in(page, mockapp_url)
    frame_named(page, "main")

    def attached_sees_the_shell() -> bool:
        return any(
            p.url == f"{mockapp_url}/" for ctx in operator_browser.contexts for p in ctx.pages
        )

    wait_until(attached_sees_the_shell, "the attached browser to see the signed-in shell")


def test_session_expiry_relogin_does_not_nest_the_frameset(page: Page, mockapp_url: str) -> None:
    """The nested-frameset trap that relogin recovery would otherwise fall into.

    Session expiry redirects whichever frame was mid-request to the sign-on
    screen, so the relogin happens inside the main frame. If that POST returned
    the whole shell, nav and main would exist twice over and an honest recovery
    would surface as LOCATOR_AMBIGUOUS. The frame re-enters the main pane only.
    """
    page.goto(f"{mockapp_url}/login?inject={Inject.SESSION_EXPIRED.value}")
    sign_in(page, mockapp_url)
    main = frame_named(page, "main")

    main.fill("input[name=F_MBRID]", "10003")
    main.click("input[type=submit]")
    main.wait_for_url("**/login")

    # Relogin, inside the frame this time.
    main.fill("input[name=F_USRID]", "operator")
    main.fill("input[name=F_PWD]", "operator")
    main.click("input[type=submit]")
    main.wait_for_url("**/search")

    assert sorted(f.name for f in page.frames if f.name) == ["main", "nav"]
    assert frame_named(page, "nav").get_by_role("link", name="Member Search").count() == 1
    assert main.get_by_role("button", name="Search").count() == 1

    # One-shot: the expiry does not fire a second time.
    main.fill("input[name=F_MBRID]", "10003")
    main.click("input[type=submit]")
    savings = format_currency(MEMBERS["10003"].savings)
    wait_until(lambda: savings in settled_content(main), "the member detail screen")
