"""The adapters this build has, as data: which target kinds each drives and
what it can do.

Held apart from the adapters themselves so that a check made before anything
starts (``cua replay`` refusing an incompatible capability, ``cua workflow
check``, the catalog) needs no browser library imported. Each adapter class
exposes the same descriptor as its ``descriptor``, and a test holds the two to
each other.
"""

from __future__ import annotations

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

ADAPTERS: dict[str, SurfaceDescriptor] = {kind: PLAYWRIGHT for kind in PLAYWRIGHT.targets}
"""``target.surface`` kind → the adapter that drives it. ``desktop`` is the
declared but unbuilt case: it needs a ``Surface`` over UIA or AX, and until one
is registered here a capability that names it is refused before anything
starts, rather than handed to the wrong adapter."""


def adapter_for(kind: str) -> SurfaceDescriptor | None:
    return ADAPTERS.get(kind)
