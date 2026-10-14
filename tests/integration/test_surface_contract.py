"""Browser implementation of the shared live Surface acceptance suite."""

import os
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from cua.demo.runner import server
from cua.surface.adapters import PLAYWRIGHT
from cua.surface.playwright_surface import PlaywrightSurface
from cua.surface.protocol import Navigate
from tests.conftest import REPO_ROOT, browser_launch_error, free_port
from tests.contracts.surface import SurfaceCase, SurfaceContract

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def contract_url():
    port = free_port()
    url = f"http://127.0.0.1:{port}/panel"
    with server(
        ["uvicorn", "tests.contracts.web_target:app", "--host", "127.0.0.1", "--port", str(port)],
        url,
        REPO_ROOT,
        dict(os.environ),
    ):
        yield url


@pytest.fixture
def contract(contract_url, request):
    def run(check, inject=""):
        def owned():
            try:
                # An owned Playwright context fixes device scale to 1, matching
                # viewport pixel guards on both Linux and Windows display scales.
                surface = PlaywrightSurface.launch()
            except Exception as exc:
                browser_launch_error(exc, required=request.config.getoption("--require-browser"))
                raise
            browser = surface._page.context.browser
            assert browser is not None
            endpoint = surface.expose().cdp_url

            def closed():
                if browser.is_connected():
                    return False
                try:
                    httpx.get(endpoint + "/json/version", timeout=1)
                except httpx.TransportError:
                    return True
                return False

            case = SurfaceCase(
                surface,
                PLAYWRIGHT,
                contract_url,
                closed,
                # A synthetic operator changes the page behind the observation.
                lambda text: surface._page.get_by_role(
                    "textbox", name="First number", exact=True
                ).fill(text),
            )
            try:
                with surface:
                    surface.act(
                        Navigate(url=contract_url + (f"?inject={inject}" if inject else ""))
                    )
                    check(case)
            finally:
                assert case.closed(), "owned browser survived context exit"

        # Other integration fixtures may have a sync Playwright loop on the
        # pytest thread. Own this complete surface/check on one separate thread.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(owned).result(timeout=90)

    return run


class TestPlaywrightContract(SurfaceContract):
    pass
