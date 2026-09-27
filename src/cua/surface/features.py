"""What a surface can do, named, so a capability can say what it needs.

A capability recorded against a web page leans on things a web page has and
another kind of target may not: named frames, pixel boxes, a URL, native
dialogs. Replaying it against a surface without one of them does not fail
loudly — a ``near_text`` rung with no geometry matches nothing, or worse,
something; a frame-scoped rung on a surface with no frames silently searches
the whole screen. So the gap is named on both sides, and compared before the
first action: a surface publishes the features it has (``SurfaceDescriptor``),
a capability states the features it uses (``surface_requirements``), and a
run whose surface lacks one is refused, not attempted.

The vocabulary is closed. A feature is added here, with its meaning, before
any adapter may claim it or any artifact require it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

SurfaceFeature = Literal[
    "accessibility_tree",
    "geometry",
    "fixed_viewport",
    "frames",
    "locations",
    "document_status",
    "forms",
    "keyboard",
    "dialogs",
    "screenshots",
    "pointer",
    "egress_control",
    "session_handoff",
]

FEATURES: tuple[SurfaceFeature, ...] = get_args(SurfaceFeature)

FEATURE_MEANINGS: dict[SurfaceFeature, str] = {
    "accessibility_tree": "nodes with a role, an accessible name and a value",
    "geometry": "a box for each control and label, in one coordinate space",
    "fixed_viewport": "pixel positions are measured in a viewport that is the same at replay "
    "as at recording, and a pixel rung is refused when it is not",
    "frames": "named sub-documents (a frameset's panes) that nodes and scopes can name",
    "locations": "the target can be sent to a location and reports where it is",
    "document_status": "the load status of each document (an HTTP 500 page has no ARIA)",
    "forms": "text can be typed into a control and an option selected",
    "keyboard": "keys can be pressed",
    "dialogs": "native dialogs are raised, answered and reported",
    "screenshots": "a screenshot with masks painted in before it is captured",
    "pointer": "a click at a point of the screen, refused if the pixels there changed "
    "since the point was chosen",
    "egress_control": "what the target may send can be restricted to allowed origins",
    "session_handoff": "the live session can be published for a person to take over",
}


class SurfaceDescriptor(BaseModel):
    """``surface.descriptor``: which adapter this is, and what it can do.

    Called ``features`` rather than capabilities, because in this codebase a
    capability is the artifact that runs *on* a surface.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str
    """Of the adapter's contract. Bumped when a feature it claims changes meaning."""
    targets: tuple[str, ...] = ()
    """The ``target.surface`` kinds this adapter drives."""
    features: frozenset[SurfaceFeature] = Field(default_factory=frozenset)

    def missing(self, required: Iterable[SurfaceFeature]) -> list[SurfaceFeature]:
        """Required features this surface does not have, in vocabulary order."""
        wanted = set(required)
        return [f for f in FEATURES if f in wanted and f not in self.features]

    def summary(self) -> dict[str, object]:
        """For ``run.json`` and the CLI: plain, sorted, stable."""
        return {
            "name": self.name,
            "version": self.version,
            "targets": list(self.targets),
            "features": ordered(self.features),
        }


def ordered(features: Iterable[SurfaceFeature]) -> list[SurfaceFeature]:
    """Features in vocabulary order, once each: how they are written down."""
    have = set(features)
    return [f for f in FEATURES if f in have]
