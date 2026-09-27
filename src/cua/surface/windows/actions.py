"""The surface's actions, on a desktop control.

Each takes the node (what was observed) and the element (checked live, by
``locator.confirm``) and does what a person would, in the way the target
answers without blocking the surface (see ``uia``). A control that refuses —
disabled, read-only, no such option — raises ``ActionFailed``, the same
fault the web adapter reports.
"""

from __future__ import annotations

from cua.surface.protocol import CONTROL_ROLES, ActionFailed, Node
from cua.surface.windows import uia
from cua.surface.windows.perception import RawElement

_POSTED_CLICK_CLASSES = ("BUTTON",)
"""Win32 classes whose click is posted (``BM_CLICK``): push buttons, checkboxes
and radio buttons, in any framework built on the common controls."""


def click(client: uia.Client, node: Node, element: RawElement) -> None:
    _enabled(node, element)
    if element.hwnd and _posts_click(element):
        if not uia.post_click(element.hwnd):
            raise ActionFailed(f"{node.label} did not take the click")
        return
    if not client.invoke(element.handle):
        raise ActionFailed(f"{node.label} cannot be clicked: it has no invoke, toggle or select")


def type_text(client: uia.Client, node: Node, element: RawElement, text: str, clear: bool) -> None:
    _enabled(node, element)
    if not clear:
        text = (client.current_value(element.handle) or "") + text
    if not client.set_value(element.handle, text):
        raise ActionFailed(f"{node.label} does not take text (read-only, or not a text field)")


def select(node: Node, element: RawElement, option: str) -> None:
    _enabled(node, element)
    if node.role not in ("combobox", "listbox") or not element.hwnd:
        raise ActionFailed(f"{node.label} is not a list to select from")
    if not uia.select_option(element.hwnd, option, listbox=node.role == "listbox"):
        raise ActionFailed(f"{node.label} has no option {option!r}")


def press(hwnd: int, key: str) -> None:
    try:
        posted = uia.post_key(hwnd, key)
    except KeyError:
        raise ActionFailed(f"no key named {key!r} on this surface") from None
    if not posted:
        raise ActionFailed(f"the key {key!r} was not delivered")


def read(client: uia.Client, node: Node, element: RawElement) -> str:
    """Live, as on the web: a control's value (empty when it is empty, never
    its label), and otherwise the text it shows."""
    if node.role in ("checkbox", "radio"):
        return "checked" if node.attrs.get("checked") else "unchecked"
    if node.role in CONTROL_ROLES:
        return client.current_value(element.handle) or ""
    return client.current_value(element.handle) or element.name


def _enabled(node: Node, element: RawElement) -> None:
    if not element.enabled:
        raise ActionFailed(f"{node.label} is disabled")


def _posts_click(element: RawElement) -> bool:
    name = element.class_name.upper()
    return any(c in name for c in _POSTED_CLICK_CLASSES)
