"""The identical acceptance suite, driving real DeskCalc through Windows UIA."""

import pytest

from cua.surface.adapters import WINDOWS_UIA
from cua.surface.protocol import Navigate
from cua.surface.windows import uia
from cua.surface.windows.adapter import WindowsSurface
from tests.contracts.surface import SurfaceCase, SurfaceContract
from tests.desktop.conftest import lines

pytestmark = pytest.mark.desktop


@pytest.fixture
def contract(desk, ledger):
    def run(check, inject=""):
        # Frames/error variants belong to optional browser features. The common
        # location/refusal assertion still runs against a normal desktop window.
        fault = inject if inject in ("ambiguous", "modal") else ""
        surface = None
        pid = None
        try:
            with WindowsSurface.launch(desk) as surface:
                surface.act(Navigate(url="uia://deskcalc/" + (f"?inject={fault}" if fault else "")))
                pid = surface.pid
                assert pid is not None
                case = SurfaceCase(
                    surface,
                    WINDOWS_UIA,
                    "uia://deskcalc/",
                    lambda: surface.pid is None and not uia.top_windows(pid),
                )
                check(case)
        finally:
            if surface is not None and pid is not None:
                assert surface.pid is None and not uia.top_windows(pid)
                assert lines(ledger) == [], "contract unexpectedly recorded a commit"

    return run


class TestWindowsContract(SurfaceContract):
    pass
