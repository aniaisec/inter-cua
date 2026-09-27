"""The Windows UI Automation surface, the parts that run on any platform:
reading a UIA snapshot into an observation, checking a node before acting,
and what the rest of the system needs to know about desktop targets.

The live adapter against DeskCalc is ``tests/desktop``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from cua.agent.script import load_script
from cua.agent.tools import SelectCall, parse_call, tool_definitions
from cua.artifact import requirements
from cua.artifact.store import load
from cua.surface.adapters import WINDOWS_UIA
from cua.surface.locators import RoleName, ladder_for, resolve_ladder
from cua.surface.protocol import Node, PerceptionDrift, Rect
from cua.surface.windows import perception as p
from cua.surface.windows.locator import confirm
from cua.surface.windows.perception import RawElement, perceive, signature
from cua.tenant import DesktopApp, Tenant, load_tenant

REPO = Path(__file__).resolve().parents[2]
WHEN = datetime(2026, 9, 27, tzinfo=UTC)


def el(ct: int, name: str = "", rect=(0, 0, 0, 0), **kw) -> RawElement:
    return RawElement(control_type=ct, name=name, rect=tuple(map(float, rect)), **kw)


def deskcalc(**changes) -> RawElement:
    """A DeskCalc window as UIA reports it (measured), window at (120, 120)."""
    combo = el(
        p.COMBOBOX,
        "Operation",
        (278, 261, 438, 282),
        value="Add",
        hwnd=5,
        children=[
            el(p.TEXT, "Operation", (3, 3, 140, 18)),
            el(p.BUTTON, "Open", (422, 262, 437, 281)),
        ],
    )
    menu = el(
        p.MENUBAR,
        "",
        (128, 151, 588, 175),
        children=[
            el(
                p.MENUITEM,
                "File",
                (134, 153, 171, 173),
                children=[el(p.MENUITEM, "Exit", offscreen=True)],
            ),
        ],
    )
    children = [
        el(
            p.TITLEBAR,
            "",
            (144, 123, 588, 151),
            children=[el(p.BUTTON, "Close", (541, 121, 589, 151))],
        ),
        menu,
        el(p.TEXT, "First number", (148, 194, 268, 214)),
        el(p.EDIT, "First number", (278, 191, 438, 211), value="12.5", hwnd=1),
        el(p.TEXT, "Password", (148, 229, 268, 249)),
        el(p.EDIT, "Password", (278, 226, 438, 246), value="hunter2", password=True, hwnd=2),
        combo,
        el(p.CHECKBOX, "Round to cents", (278, 294, 438, 316), toggle=1, hwnd=3),
        el(
            p.BUTTON,
            "Calculate",
            (278, 326, 378, 354),
            enabled=False,
            hwnd=4,
            automation_id="97466074",
        ),
        el(p.WINDOW, "DeskCalc", (200, 200, 400, 300), class_name=p.DIALOG_CLASS),
        el(p.PANE, "", (128, 559, 588, 581), automation_id="statusStrip"),
    ]
    return el(p.WINDOW, changes.pop("title", "DeskCalc"), (120, 120, 596, 589), children=children)


def seen(window: RawElement | None = None, start: int = 1):
    return perceive(window or deskcalc(), app="deskcalc", start_index=start, observed_at=WHEN)


# --------------------------------------------------------------------------
# Perception
# --------------------------------------------------------------------------


def test_uia_elements_become_the_same_nodes_the_web_produces() -> None:
    obs = seen().observation
    assert obs.location == "uia://deskcalc/DeskCalc" and obs.title == "DeskCalc"
    assert [f.model_dump() for f in obs.frames] == [
        {"name": "", "url": "uia://deskcalc/DeskCalc", "status": None}
    ]
    assert obs.viewport.w == 476 and obs.viewport.h == 469
    shown = [(n.role, n.name) for n in obs.nodes]
    assert shown == [
        ("window", "DeskCalc"),
        ("menubar", ""),
        ("menuitem", "File"),
        ("text", "First number"),
        ("textbox", "First number"),
        ("text", "Password"),
        ("textbox", "Password"),
        ("combobox", "Operation"),
        ("checkbox", "Round to cents"),
        ("button", "Calculate"),
        ("generic", ""),
    ]


def test_chrome_hidden_items_a_combo_boxs_parts_and_message_boxes_are_left_out() -> None:
    names = {n.name for n in seen().observation.nodes}
    assert "Close" not in names  # title bar
    assert "Exit" not in names  # a collapsed menu's item has no box
    assert "Open" not in names  # the combo box carries its own value
    assert sum(1 for n in seen().observation.nodes if n.role == "window") == 1  # no dialog


def test_boxes_are_window_relative_and_parents_ordinals_and_refs_follow_the_tree() -> None:
    obs = seen(start=40).observation
    first = next(n for n in obs.nodes if n.role == "textbox")
    assert first.bbox == Rect(x=158, y=71, w=160, h=20)
    assert first.ref == "n44" and first.parent == "n40" and first.ordinal == 0
    assert obs.node("n42").parent == "n41"  # File sits in the menu bar
    assert [n.ordinal for n in obs.nodes if n.role == "textbox"] == [0, 1]
    # Moving the window does not move anything a capability recorded.
    moved = deskcalc()

    def shift(e: RawElement) -> None:
        x0, y0, x1, y1 = e.rect
        e.rect = (x0 + 500, y0 - 100, x1 + 500, y1 - 100)
        for child in e.children:
            shift(child)

    shift(moved)
    again = seen(moved).observation
    assert [n.bbox for n in again.nodes[1:]] == [n.bbox for n in seen().observation.nodes[1:]]


def test_state_is_reported_as_the_web_reports_it_and_a_password_is_never_read() -> None:
    obs = seen().observation
    by = {(n.role, n.name): n for n in obs.nodes}
    assert by[("button", "Calculate")].attrs == {"disabled": "true"}  # handle id dropped
    assert by[("checkbox", "Round to cents")].attrs == {"checked": "true"}
    assert by[("checkbox", "Round to cents")].value is None
    assert by[("combobox", "Operation")].value == "Add"
    assert by[("textbox", "Password")].value is None
    assert by[("generic", "")].attrs == {"automation_id": "statusStrip"}


def test_the_ladder_and_the_conditions_read_a_desktop_observation_unchanged() -> None:
    obs = seen().observation
    node = next(n for n in obs.nodes if n.role == "textbox" and n.name == "First number")
    ladder = ladder_for(node, obs)
    assert ladder[0] == RoleName(role="textbox", name="First number", within=ladder[0].within)
    assert resolve_ladder(ladder, obs).ref == node.ref  # type: ignore[union-attr]
    # Read for its value, the field is still named by its label: the value is
    # held apart from the name, unlike a web table cell's.
    assert isinstance(ladder_for(node, obs, for_value=True)[0], RoleName)


def test_the_signature_changes_with_the_screen_and_only_with_it() -> None:
    assert signature(deskcalc()) == signature(deskcalc())
    changed = deskcalc()
    changed.children[3].value = "13"
    assert signature(changed) != signature(deskcalc())


# --------------------------------------------------------------------------
# Checked before acting
# --------------------------------------------------------------------------


def _node(**kw) -> Node:
    return Node(
        ref="n5", role="button", name="Calculate", bbox=Rect(x=158, y=206, w=100, h=28), **kw
    )


def test_the_live_element_must_still_be_the_observed_control() -> None:
    observed = el(p.BUTTON, "Calculate", (278, 326, 378, 354))
    same = confirm(_node(), observed, lambda raw: raw, origin=(120, 120))
    assert same is observed
    renamed = el(p.BUTTON, "Compute", (278, 326, 378, 354))
    with pytest.raises(PerceptionDrift, match="now named 'Compute'"):
        confirm(_node(), observed, lambda _: renamed, origin=(120, 120))
    moved = el(p.BUTTON, "Calculate", (278, 366, 378, 394))
    with pytest.raises(PerceptionDrift, match="moved"):
        confirm(_node(), observed, lambda _: moved, origin=(120, 120))
    # The whole window moving is not the control moving.
    assert confirm(_node(), observed, lambda _: moved, origin=(120, 160)) is moved

    def gone(_: RawElement) -> RawElement:
        raise OSError("element not available")

    with pytest.raises(PerceptionDrift, match="no longer on screen"):
        confirm(_node(), observed, gone, origin=(120, 120))


# --------------------------------------------------------------------------
# What the rest of the system knows about desktop targets
# --------------------------------------------------------------------------


def test_a_desktop_tenant_says_how_its_application_starts() -> None:
    desk = load_tenant("desk", root=REPO / "tenants")
    assert desk.base_url == "uia://deskcalc" and desk.origin == "uia://deskcalc"
    assert desk.desktop is not None and desk.desktop.launch[-1] == "deskapp/deskcalc.ps1"
    with pytest.raises(ValidationError, match=r"desktop\.launch"):
        Tenant(id="x", app_family="f", base_url="uia://app")
    with pytest.raises(ValidationError, match="uia://"):
        Tenant(id="x", app_family="f", base_url="http://h", desktop=DesktopApp(launch=["a"]))


@pytest.mark.parametrize("name", ["deskcalc_compute", "deskcalc_record"])
def test_the_desktop_capabilities_need_only_what_the_uia_surface_has(name: str) -> None:
    cap = load(REPO / "capabilities" / f"{name}.json")
    assert cap.target.surface == "desktop" and cap.schema_version == "1.2"
    run = requirements.for_run(cap, handoff=False, screenshots=False)
    assert run.run == []  # no egress guard asked of a desktop target
    assert WINDOWS_UIA.missing(run.all) == []
    # Screenshots and a person to hand over to are what it cannot give.
    assert WINDOWS_UIA.missing(requirements.for_run(cap, handoff=True).all) == [
        "screenshots",
        "session_handoff",
    ]


def test_a_web_run_still_needs_the_egress_guard() -> None:
    web = load(REPO / "capabilities" / "member_savings_balance.json")
    assert "egress_control" in requirements.for_run(web, handoff=False).run


def test_discovery_can_choose_an_option() -> None:
    names = [t.name for t in tool_definitions([])]
    assert names[:3] == ["click", "type", "select"]
    call = parse_call("select", {"ref": "n3", "text": "Divide", "reason": "why"})
    assert isinstance(call, SelectCall) and call.text == "Divide"
    script = load_script(REPO / "scripts" / "discovery" / "deskcalc_compute.yaml")
    assert [s.tool for s in script.steps] == ["type", "type", "select", "click", "done"]
