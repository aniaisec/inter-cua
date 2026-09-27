"""The adapters this build has, as data: which target kinds each drives and
what it can do.

Held apart from the adapters themselves so that a check made before anything
starts (``cua replay`` refusing an incompatible capability, ``cua workflow
check``, the catalog) needs no browser library imported. Each adapter class
exposes the same descriptor as its ``descriptor``, and a test holds the two to
each other.
"""

from __future__ import annotations

import sys

from cua.surface.features import FEATURES, SurfaceDescriptor

PLAYWRIGHT = SurfaceDescriptor(
    name="playwright",
    version="1",
    targets=("web", "legacy_web"),
    features=frozenset(FEATURES),
)
"""A Chromium page. A legacy web app is still a web page, frameset and all,
so both web kinds share it. It has every feature in the vocabulary today; that
is a fact about this adapter, not a default."""

WINDOWS_UIA = SurfaceDescriptor(
    name="windows-uia",
    version="1",
    targets=("desktop",),
    features=frozenset(
        {
            "accessibility_tree",
            "geometry",
            "fixed_viewport",
            "locations",
            "forms",
            "keyboard",
            "dialogs",
        }
    ),
)
"""A Windows application, through UI Automation (``cua.surface.windows``).

Boxes are measured from the window's own corner and the viewport is the
window's size, so a pixel position means the same thing wherever the window
sits, and ``resolve_ladder`` refuses a ``bbox`` rung when the size or the scale
differs from the recording's (``fixed_viewport``).

What it does not claim, and why:

* ``frames``: a window has no named sub-documents.
* ``document_status``: there is no HTTP status behind a window.
* ``screenshots``: a mask cannot yet be painted in before the capture, so no
  screenshot is taken at all rather than one cleaned up afterwards.
* ``egress_control``: what a native application sends is outside what a UI
  surface can govern.
* ``session_handoff``: there is no endpoint a person could attach to.

Each missing feature is a capability this adapter will refuse, not one it
will run badly."""

ADAPTERS: dict[str, SurfaceDescriptor] = {kind: PLAYWRIGHT for kind in PLAYWRIGHT.targets}
"""``target.surface`` kind → the adapter that drives it. ``desktop`` is driven
by UI Automation on Windows only. Elsewhere it has no adapter, and a
capability that names it is refused before anything starts rather than handed
to the wrong one."""
if sys.platform == "win32":
    ADAPTERS["desktop"] = WINDOWS_UIA


def adapter_for(kind: str) -> SurfaceDescriptor | None:
    return ADAPTERS.get(kind)
