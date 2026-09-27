"""UI Automation elements, read into the same ``Observation`` the web produces.

No second model of the screen: a desktop control becomes a ``Node`` with a
ref, a role from the web's vocabulary, a name, a value, a box and a parent, so
the locator ladder, the conditions and the recorder read it exactly as they
read a web page. The mapping is a pure function of a snapshot
(``RawElement``), which is what lets it be tested on any platform, and lets a
desktop observation be written to evidence and re-read like any other.

Choices the mapping makes:

* **Roles.** UIA control types map onto the ARIA roles the rest of the system
  already knows (``Edit`` → ``textbox``, ``ListItem`` → ``option``). A control
  type with no counterpart is ``generic``: kept in the tree as structure, but
  never addressed.
* **What is left out.** The title bar and scroll bars (window chrome, not the
  application), a combo box's own parts (the combo box carries its value),
  and anything off screen: a collapsed menu's items have no box, and a web page
  does not show hidden elements either.
* **Boxes** are measured from the window's corner, and the viewport is the
  window's size, so a position means the same wherever the window sits.
* **State.** Disabled and checked are ``attrs``, as the web's snapshot reports
  ``[disabled]`` and ``[checked]``; a checkbox has no value.
* **Location** is ``uia://<app>/<window title>``: what ``location_matches``
  reads on a desktop, as the web's is the page URL.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from cua.surface import a11y
from cua.surface.protocol import (
    DEFAULT_CONFIG,
    TOP_FRAME,
    DialogEvent,
    FrameInfo,
    Node,
    Observation,
    Rect,
    SurfaceConfig,
    Viewport,
)

# UIA control type ids (UIAutomationClient.h).
BUTTON = 50000
CALENDAR = 50001
CHECKBOX = 50002
COMBOBOX = 50003
EDIT = 50004
HYPERLINK = 50005
IMAGE = 50006
LISTITEM = 50007
LIST = 50008
MENU = 50009
MENUBAR = 50010
MENUITEM = 50011
PROGRESSBAR = 50012
RADIOBUTTON = 50013
SCROLLBAR = 50014
SLIDER = 50015
SPINNER = 50016
STATUSBAR = 50017
TAB = 50018
TABITEM = 50019
TEXT = 50020
TOOLBAR = 50021
TOOLTIP = 50022
TREE = 50023
TREEITEM = 50024
CUSTOM = 50025
GROUP = 50026
THUMB = 50027
DATAGRID = 50028
DATAITEM = 50029
DOCUMENT = 50030
SPLITBUTTON = 50031
WINDOW = 50032
PANE = 50033
HEADER = 50034
HEADERITEM = 50035
TABLE = 50036
TITLEBAR = 50037
SEPARATOR = 50038

ROLES: dict[int, str] = {
    BUTTON: "button",
    SPLITBUTTON: "button",
    CHECKBOX: "checkbox",
    COMBOBOX: "combobox",
    EDIT: "textbox",
    DOCUMENT: "textbox",
    HYPERLINK: "link",
    IMAGE: "img",
    LISTITEM: "option",
    LIST: "listbox",
    MENU: "menu",
    MENUBAR: "menubar",
    MENUITEM: "menuitem",
    PROGRESSBAR: "progressbar",
    RADIOBUTTON: "radio",
    SLIDER: "slider",
    SPINNER: "spinbutton",
    STATUSBAR: "status",
    TAB: "tablist",
    TABITEM: "tab",
    TEXT: "text",
    TOOLBAR: "toolbar",
    TREE: "tree",
    TREEITEM: "treeitem",
    GROUP: "group",
    DATAGRID: "table",
    TABLE: "table",
    DATAITEM: "row",
    HEADER: "row",
    HEADERITEM: "columnheader",
    WINDOW: "window",
}
"""UIA control type → role. Anything else (pane, custom, thumb) is ``generic``."""

SKIPPED: frozenset[int] = frozenset({TITLEBAR, SCROLLBAR, THUMB, TOOLTIP, SEPARATOR})
"""Window chrome: dropped with everything inside it."""

DIALOG_CLASS = "#32770"
"""The Win32 class of a message box and of a standard dialog. These are the
desktop's native dialogs: the surface answers and reports them, as the web
adapter does ``alert``/``confirm``, instead of showing them as screen content."""

TOGGLE_ON = 1


@dataclass
class RawElement:
    """One UI Automation element as read, with its subtree.

    ``handle`` is the live element, opaque here: the adapter keeps it to act on
    the node this element becomes, and perception never looks at it."""

    control_type: int
    name: str = ""
    automation_id: str = ""
    class_name: str = ""
    rect: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    """left, top, right, bottom, in screen pixels."""
    value: str | None = None
    enabled: bool = True
    toggle: int | None = None
    """``ToggleState``: 0 off, 1 on, 2 indeterminate; None without the pattern."""
    password: bool = False
    offscreen: bool = False
    hwnd: int = 0
    children: list[RawElement] = field(default_factory=list)
    handle: object = None

    @property
    def empty(self) -> bool:
        left, top, right, bottom = self.rect
        return right <= left or bottom <= top


@dataclass
class Perceived:
    observation: Observation
    handles: dict[str, RawElement]
    """ref → the element it was minted for, for acting on it."""


def location_of(app: str, title: str) -> str:
    return f"uia://{app}/{title}"


def perceive(
    window: RawElement,
    *,
    app: str,
    start_index: int,
    dialogs: Sequence[DialogEvent] = (),
    config: SurfaceConfig = DEFAULT_CONFIG,
    observed_at: datetime | None = None,
) -> Perceived:
    """The window as an observation, numbering refs from ``start_index``."""
    left, top, right, bottom = window.rect
    viewport = Viewport(w=max(0, round(right - left)), h=max(0, round(bottom - top)))
    location = location_of(app, window.name)

    nodes: list[Node] = []
    handles: dict[str, RawElement] = {}
    counts: dict[str, int] = {}

    def visit(element: RawElement, parent: str | None) -> None:
        role = ROLES.get(element.control_type, "generic")
        ref = f"n{start_index + len(nodes)}"
        ordinal = counts.get(role, 0)
        counts[role] = ordinal + 1
        nodes.append(
            Node(
                ref=ref,
                role=role,
                name=element.name or "",
                value=_value(element, role),
                frame=TOP_FRAME,
                bbox=_box(element, left, top),
                parent=parent,
                ordinal=ordinal,
                attrs=_attrs(element),
            )
        )
        handles[ref] = element
        for child in _shown_children(element):
            visit(child, ref)

    visit(window, None)
    observation = Observation(
        observed_at=observed_at or datetime.now(UTC),
        location=location,
        title=window.name,
        viewport=viewport,
        frames=[FrameInfo(name=TOP_FRAME, url=location)],
        nodes=a11y.annotate_near_text(nodes, config),
        dialogs=list(dialogs),
    )
    return Perceived(observation, handles)


def signature(window: RawElement) -> tuple[object, ...]:
    """What changes when the screen changes: for deciding the window has
    settled, without minting refs."""
    return tuple(
        (e.control_type, e.name, e.value, e.enabled, e.toggle, e.rect) for e in _shown(window)
    )


# -- helpers ---------------------------------------------------------------------


def _shown_children(element: RawElement) -> list[RawElement]:
    if element.control_type == COMBOBOX:
        return []  # its edit, its button and its list are the combo box itself
    return [
        c
        for c in element.children
        if c.control_type not in SKIPPED
        and not c.offscreen
        and not c.empty
        # UIA shows a window's message box as its child; the surface answers
        # it as a dialog instead of showing it as content.
        and c.class_name != DIALOG_CLASS
    ]


def _shown(element: RawElement) -> Iterator[RawElement]:
    yield element
    for child in _shown_children(element):
        yield from _shown(child)


def _value(element: RawElement, role: str) -> str | None:
    if element.password:
        return None  # never read a password back, even into memory
    if role in ("checkbox", "radio", "button", "menuitem", "window"):
        return None
    return element.value if element.value else None


def _box(element: RawElement, left: float, top: float) -> Rect | None:
    if element.empty:
        return None
    x0, y0, x1, y1 = element.rect
    return Rect(x=x0 - left, y=y0 - top, w=x1 - x0, h=y1 - y0)


def _attrs(element: RawElement) -> dict[str, str]:
    attrs: dict[str, str] = {}
    if not element.enabled:
        attrs["disabled"] = "true"
    if element.toggle == TOGGLE_ON:
        attrs["checked"] = "true"
    if element.automation_id and not element.automation_id.isdigit():
        # A WinForms automation id is the window handle, different on every
        # launch: not an identity anything should be keyed on.
        attrs["automation_id"] = element.automation_id
    return attrs
