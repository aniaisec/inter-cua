"""The COM and Win32 layer under the Windows surface. Windows only.

Everything that talks to UI Automation or posts a window message is here; the
rest of the package deals in ``RawElement`` and nodes.

Two things measured against the target decide how this layer acts:

* **Input is posted, never invoked.** ``InvokePattern.Invoke`` on a button
  whose handler opens a modal message box does not return until someone
  closes the box (measured: the call hung until the process was killed). The
  surface must be free to see and answer that box, so a button is clicked by
  posting ``BM_CLICK``, which returns at once, as does a posted key.
* **A WinForms combo box has no ExpandCollapse or SelectionItem pattern**, only
  a read-only value. An option is selected the Win32 way, ``CB_FINDSTRINGEXACT``
  and ``CB_SETCURSEL``, and the owner is told with ``CBN_SELCHANGE`` so the
  application's own change handler runs, as it would for a person.
"""

from __future__ import annotations

import ctypes
import platform
from ctypes import wintypes
from typing import Any

from cua.surface.windows.perception import DIALOG_CLASS, RawElement

if platform.system() != "Windows":  # pragma: no cover - imported only on Windows
    raise ImportError("cua.surface.windows.uia needs Windows UI Automation")

import comtypes
import comtypes.client

comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen import UIAutomationClient as UIA  # noqa: E402

COMError = comtypes.COMError

# These attributes exist only on Windows, including in typeshed. Runtime
# platform detection above preserves the guard while letting mypy check this
# module and its exports on every host.
_user32 = getattr(ctypes, "WinDLL")("user32", use_last_error=True)  # noqa: B009
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.SendMessageW.restype = ctypes.c_ssize_t
_user32.GetParent.argtypes = [wintypes.HWND]
_user32.GetParent.restype = wintypes.HWND
_user32.GetDlgCtrlID.argtypes = [wintypes.HWND]
_user32.GetDlgCtrlID.restype = ctypes.c_int
_user32.GetDpiForWindow.argtypes = [wintypes.HWND]
_user32.GetDpiForWindow.restype = wintypes.UINT
_ENUM_PROC = getattr(ctypes, "WINFUNCTYPE")(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)  # noqa: B009
_user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int

BM_CLICK = 0x00F5
WM_COMMAND = 0x0111
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
CB_FINDSTRINGEXACT = 0x0158
CB_SETCURSEL = 0x014E
CBN_SELCHANGE = 1
LB_FINDSTRINGEXACT = 0x01A2
LB_SETCURSEL = 0x0186
LBN_SELCHANGE = 1
NOT_FOUND = -1

VIRTUAL_KEYS: dict[str, int] = {
    "Enter": 0x0D,
    "Tab": 0x09,
    "Escape": 0x1B,
    "Space": 0x20,
    "Backspace": 0x08,
    "Delete": 0x2E,
    "Home": 0x24,
    "End": 0x23,
    "ArrowLeft": 0x25,
    "ArrowUp": 0x26,
    "ArrowRight": 0x27,
    "ArrowDown": 0x28,
    "F1": 0x70,
    "F5": 0x74,
}

_PROPERTIES = (
    UIA.UIA_ControlTypePropertyId,
    UIA.UIA_NamePropertyId,
    UIA.UIA_AutomationIdPropertyId,
    UIA.UIA_ClassNamePropertyId,
    UIA.UIA_BoundingRectanglePropertyId,
    UIA.UIA_IsEnabledPropertyId,
    UIA.UIA_IsOffscreenPropertyId,
    UIA.UIA_IsPasswordPropertyId,
    UIA.UIA_NativeWindowHandlePropertyId,
    UIA.UIA_ProcessIdPropertyId,
    UIA.UIA_IsValuePatternAvailablePropertyId,
    UIA.UIA_ValueValuePropertyId,
    UIA.UIA_IsTogglePatternAvailablePropertyId,
    UIA.UIA_ToggleToggleStatePropertyId,
)


class Client:
    """One UI Automation client, with the cache request every snapshot uses:
    the whole control-view subtree and its properties in one cross-process
    round trip, rather than one per property per element."""

    def __init__(self) -> None:
        self.uia: Any = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        cache = self.uia.CreateCacheRequest()
        for prop in _PROPERTIES:
            cache.AddProperty(prop)
        cache.TreeScope = UIA.TreeScope_Subtree
        cache.TreeFilter = self.uia.ControlViewCondition
        self._cache = cache

    # -- finding windows ---------------------------------------------------------

    def element(self, hwnd: int) -> Any:
        return self.uia.ElementFromHandle(hwnd)

    def main_window(self, pid: int) -> Any | None:
        for hwnd, class_name in top_windows(pid):
            if class_name != DIALOG_CLASS:
                return self.element(hwnd)
        return None

    def dialogs_of(self, pid: int) -> list[Any]:
        """The process's message boxes. Found by window class through Win32,
        never by a UI Automation search from the desktop root, which asks every
        window on the machine and waits on any that is not responding
        (measured: a hang)."""
        return [self.element(h) for h, c in top_windows(pid) if c == DIALOG_CLASS]

    # -- reading -----------------------------------------------------------------

    def snapshot(self, element: Any) -> RawElement:
        cached = element.BuildUpdatedCache(self._cache)
        return self._raw(cached)

    def _raw(self, e: Any) -> RawElement:
        rect = e.CachedBoundingRectangle
        children = e.GetCachedChildren()
        kids = [children.GetElement(i) for i in range(children.Length)] if children else []
        has_value = bool(e.GetCachedPropertyValue(UIA.UIA_IsValuePatternAvailablePropertyId))
        has_toggle = bool(e.GetCachedPropertyValue(UIA.UIA_IsTogglePatternAvailablePropertyId))
        password = bool(e.GetCachedPropertyValue(UIA.UIA_IsPasswordPropertyId))
        value = (
            e.GetCachedPropertyValue(UIA.UIA_ValueValuePropertyId)
            if has_value and not password
            else None
        )
        return RawElement(
            control_type=int(e.CachedControlType),
            name=e.CachedName or "",
            automation_id=e.CachedAutomationId or "",
            class_name=e.CachedClassName or "",
            rect=(float(rect.left), float(rect.top), float(rect.right), float(rect.bottom)),
            value=str(value) if isinstance(value, str) else None,
            enabled=bool(e.CachedIsEnabled),
            toggle=int(e.GetCachedPropertyValue(UIA.UIA_ToggleToggleStatePropertyId))
            if has_toggle
            else None,
            password=password,
            offscreen=bool(e.CachedIsOffscreen),
            hwnd=int(e.CachedNativeWindowHandle or 0),
            children=[self._raw(k) for k in kids],
            handle=e,
        )

    def current(self, element: Any) -> RawElement:
        """The element's properties now, without its subtree: for checking,
        just before acting, that it is still the element that was observed."""
        rect = element.CurrentBoundingRectangle
        return RawElement(
            control_type=int(element.CurrentControlType),
            name=element.CurrentName or "",
            class_name=element.CurrentClassName or "",
            rect=(float(rect.left), float(rect.top), float(rect.right), float(rect.bottom)),
            enabled=bool(element.CurrentIsEnabled),
            hwnd=int(element.CurrentNativeWindowHandle or 0),
            handle=element,
        )

    def current_value(self, element: Any) -> str | None:
        pattern = element.GetCurrentPattern(UIA.UIA_ValuePatternId)
        if not pattern:
            return None
        value = pattern.QueryInterface(UIA.IUIAutomationValuePattern).CurrentValue
        return str(value) if value is not None else None

    # -- acting ------------------------------------------------------------------

    def set_value(self, element: Any, text: str) -> bool:
        """``ValuePattern.SetValue``; False if the element has no settable value."""
        pattern = element.GetCurrentPattern(UIA.UIA_ValuePatternId)
        if not pattern:
            return False
        value = pattern.QueryInterface(UIA.IUIAutomationValuePattern)
        if value.CurrentIsReadOnly:
            return False
        value.SetValue(text)
        return True

    def invoke(self, element: Any) -> bool:
        """For an element without a window of its own (a menu item). Blocks if
        what it opens is modal; buttons are therefore posted a click instead."""
        for pattern_id, interface, call in (
            (UIA.UIA_InvokePatternId, UIA.IUIAutomationInvokePattern, "Invoke"),
            (UIA.UIA_TogglePatternId, UIA.IUIAutomationTogglePattern, "Toggle"),
            (UIA.UIA_SelectionItemPatternId, UIA.IUIAutomationSelectionItemPattern, "Select"),
            (UIA.UIA_ExpandCollapsePatternId, UIA.IUIAutomationExpandCollapsePattern, "Expand"),
        ):
            pattern = element.GetCurrentPattern(pattern_id)
            if pattern:
                getattr(pattern.QueryInterface(interface), call)()
                return True
        return False


def top_windows(pid: int) -> list[tuple[int, str]]:
    """Visible top-level windows of a process, with their Win32 class, in
    z-order. An owned message box is one of them."""
    found: list[tuple[int, str]] = []
    owner = wintypes.DWORD()
    name = ctypes.create_unicode_buffer(256)

    def visit(hwnd: int, _: int) -> bool:
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and _user32.IsWindowVisible(hwnd):
            _user32.GetClassNameW(hwnd, name, len(name))
            found.append((int(hwnd), name.value))
        return True

    _user32.EnumWindows(_ENUM_PROC(visit), 0)
    return found


def post_click(hwnd: int) -> bool:
    return bool(_user32.PostMessageW(hwnd, BM_CLICK, 0, 0))


def post_key(hwnd: int, key: str) -> bool:
    """A named key (``VIRTUAL_KEYS``), or one printable character."""
    vk = VIRTUAL_KEYS.get(key)
    if vk is None:
        if len(key) != 1:
            raise KeyError(key)
        return bool(_user32.PostMessageW(hwnd, WM_CHAR, ord(key), 0))
    down = _user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
    up = _user32.PostMessageW(hwnd, WM_KEYUP, vk, 0xC0000001)
    return bool(down and up)


def select_option(hwnd: int, option: str, *, listbox: bool) -> bool:
    """Select the item whose text is exactly ``option`` and notify the owner.
    False if there is no such item."""
    find, setsel, notify = (
        (LB_FINDSTRINGEXACT, LB_SETCURSEL, LBN_SELCHANGE)
        if listbox
        else (CB_FINDSTRINGEXACT, CB_SETCURSEL, CBN_SELCHANGE)
    )
    text = ctypes.create_unicode_buffer(option)
    index = _user32.SendMessageW(hwnd, find, ctypes.c_size_t(-1).value, ctypes.addressof(text))
    if index == NOT_FOUND:
        return False
    _user32.SendMessageW(hwnd, setsel, index, 0)
    parent = _user32.GetParent(hwnd)
    control_id = _user32.GetDlgCtrlID(hwnd) & 0xFFFF
    _user32.SendMessageW(parent, WM_COMMAND, (notify << 16) | control_id, hwnd)
    return True


def dpi_scale(hwnd: int) -> float:
    dpi = _user32.GetDpiForWindow(hwnd)
    return dpi / 96.0 if dpi else 1.0


def _safe(read: Any) -> Any:
    try:
        return read()
    except COMError:
        return None
