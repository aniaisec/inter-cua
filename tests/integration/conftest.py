"""Browser fixtures.

Handoff needs the browser to be reachable by a second party, so Chromium is
launched with a DevTools endpoint and the CDP URL is published as part of the
session handle. The automation side drives the browser it launched; the
operator side attaches over CDP to the same process.

Note for anyone porting the Node recipe: ``launch_server()`` exists only in the
Node bindings, and Python Playwright has no equivalent. The same result comes
from ``launch(args=["--remote-debugging-port=N"])`` plus ``connect_over_cdp``
for the attaching party, which is what this module does.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from tests.conftest import free_port

CDP_TIMEOUT_S = 20.0
VIEWPORT = {"width": 1280, "height": 800}


def _headed() -> bool:
    return os.environ.get("CUA_HEADED", "0") not in ("", "0", "false", "False")


@dataclass
class BrowserSession:
    """What a live automation session exposes to whoever may need to take over."""

    browser: Browser
    cdp_url: str


@pytest.fixture(scope="session")
def playwright_instance() -> Iterator[Playwright]:
    with sync_playwright() as pw:
        yield pw


@pytest.fixture(scope="session")
def browser_session(playwright_instance: Playwright) -> Iterator[BrowserSession]:
    port = free_port()
    cdp_url = f"http://127.0.0.1:{port}"
    try:
        browser = playwright_instance.chromium.launch(
            headless=not _headed(),
            args=[f"--remote-debugging-port={port}"],
        )
    except Exception as exc:  # pragma: no cover - missing browser binary
        pytest.skip(f"Chromium not available ({exc}); run `playwright install chromium`")

    deadline = time.monotonic() + CDP_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{cdp_url}/json/version", timeout=1.0).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    else:  # pragma: no cover
        browser.close()
        pytest.fail(f"CDP endpoint did not open on {cdp_url}")

    try:
        yield BrowserSession(browser=browser, cdp_url=cdp_url)
    finally:
        browser.close()


@pytest.fixture
def context(browser_session: BrowserSession) -> Iterator[BrowserContext]:
    ctx = browser_session.browser.new_context(viewport=VIEWPORT, ignore_https_errors=True)
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def page(context: BrowserContext) -> Iterator[Page]:
    p = context.new_page()
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def operator_browser(
    playwright_instance: Playwright, browser_session: BrowserSession
) -> Iterator[Browser]:
    """A second handle on the same browser, as the operator console would get it.

    Proves the handoff story is real: the attaching party sees the contexts the
    automation side created, in the state it left them. Reuses the session
    Playwright instance — a nested ``sync_playwright()`` in the same thread
    deadlocks.
    """
    attached = playwright_instance.chromium.connect_over_cdp(browser_session.cdp_url)
    try:
        yield attached
    finally:
        attached.close()
