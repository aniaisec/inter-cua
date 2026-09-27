"""``WindowsSurface`` against DeskCalc: the Surface protocol on a real window."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from cua.surface.conditions import TextPresent
from cua.surface.protocol import (
    ActionFailed,
    Click,
    ExpectDialog,
    Navigate,
    Observation,
    SelectOption,
    StaleRefError,
    SurfaceError,
    TypeText,
)
from cua.surface.windows import uia
from cua.surface.windows.adapter import WindowsSurface
from cua.tenant import Tenant
from tests.desktop.conftest import lines

pytestmark = pytest.mark.desktop


@pytest.fixture
def surface(desk: Tenant, ledger: Path) -> Iterator[WindowsSurface]:
    with WindowsSurface.launch(desk) as s:
        yield s


def start(surface: WindowsSurface, inject: str = "") -> Observation:
    surface.act(Navigate(url="uia://deskcalc/" + (f"?inject={inject}" if inject else "")))
    return surface.observe()


def ref(obs: Observation, role: str, name: str) -> str:
    return next(n.ref for n in obs.nodes if n.role == role and n.name == name)


def test_a_calculation_through_the_surface_protocol(surface: WindowsSurface) -> None:
    obs = start(surface)
    assert obs.location == "uia://deskcalc/DeskCalc"
    assert surface.descriptor.name == "windows-uia"
    surface.act(TypeText(ref=ref(obs, "textbox", "First number"), text="12.5"))
    surface.act(TypeText(ref=ref(obs, "textbox", "Second number"), text="4"))
    surface.act(SelectOption(ref=ref(obs, "combobox", "Operation"), value="Multiply"))
    surface.act(Click(ref=ref(obs, "checkbox", "Round to cents")))
    surface.act(Click(ref=ref(obs, "button", "Calculate")))
    assert surface.settle(5)
    done = surface.observe()
    result = next(n for n in done.nodes if n.role == "textbox" and n.name == "Result")
    assert result.value == "50.00"
    assert next(n for n in done.nodes if n.name == "Round to cents").attrs["checked"] == "true"
    assert next(n for n in done.nodes if n.role == "combobox").value == "Multiply"


def test_refs_are_observation_scoped(surface: WindowsSurface) -> None:
    first = start(surface)
    old = ref(first, "button", "Calculate")
    second = surface.observe()
    assert ref(second, "button", "Calculate") != old
    with pytest.raises(StaleRefError):
        surface.act(Click(ref=old))


def test_a_control_that_refuses_is_an_action_failure(surface: WindowsSurface) -> None:
    obs = start(surface, "disabled")
    calculate = next(n for n in obs.nodes if n.name == "Calculate")
    assert calculate.attrs.get("disabled") == "true"
    with pytest.raises(ActionFailed, match="disabled"):
        surface.act(Click(ref=calculate.ref))
    obs = surface.observe()
    with pytest.raises(ActionFailed, match="no option 'Modulo'"):
        surface.act(SelectOption(ref=ref(obs, "combobox", "Operation"), value="Modulo"))
    with pytest.raises(ActionFailed, match="does not take text"):
        surface.act(TypeText(ref=ref(obs, "textbox", "Result"), text="1"))


def test_a_message_box_is_answered_at_once_and_reported(surface: WindowsSurface) -> None:
    obs = start(surface, "modal")
    result = surface.act(Click(ref=ref(obs, "button", "Calculate")))
    (dialog,) = result.dialogs
    assert (dialog.kind, dialog.answer, dialog.expected) == ("alert", "dismissed", False)
    assert dialog.message == "The rate service is unavailable."
    after = surface.observe()
    assert after.dialogs == [dialog]  # on the next look as well
    assert not surface.evaluate(TextPresent(text="rate service"))  # never screen content
    # Declared beforehand, it is accepted, and says it was expected.
    surface.expect_dialog(ExpectDialog(accept=True, message_contains="rate service"))
    now = surface.observe()  # evaluate took a newer look; refs are that look's
    (declared,) = surface.act(Click(ref=ref(now, "button", "Calculate"))).dialogs
    assert (declared.answer, declared.expected) == ("accepted", True)


def test_what_the_surface_cannot_do_it_refuses(surface: WindowsSurface) -> None:
    start(surface)
    with pytest.raises(SurfaceError, match="no screenshots"):
        surface.observe(screenshot=True)
    with pytest.raises(SurfaceError, match="session_handoff"):
        surface.expose()
    with pytest.raises(ActionFailed, match="not this tenant's application"):
        surface.act(Navigate(url="uia://notepad/"))


def test_closing_the_surface_stops_the_application(desk: Tenant, ledger: Path) -> None:
    with WindowsSurface.launch(desk) as s:
        start(s)
        pid = s.pid
        assert pid is not None and uia.top_windows(pid)
    assert s.pid is None and uia.top_windows(pid) == []
    assert lines(ledger) == []
