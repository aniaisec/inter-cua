"""Perception and action, one step removed from any particular UI technology.

``protocol`` holds the vocabulary, ``a11y`` turns an accessibility snapshot
into it, ``locators`` finds a control four increasingly desperate ways,
``conditions`` says what a step is waiting for, ``evaluators`` says what that
means on the web, ``features`` names what a surface can do and ``adapters``
which surfaces this build has, and ``playwright_surface`` is the one
implementation that touches a browser.
"""

from cua.surface.adapters import ADAPTERS, PLAYWRIGHT, adapter_for
from cua.surface.conditions import (
    AllOf,
    AnyOf,
    Condition,
    DialogRaised,
    ErrorBannerPresent,
    LocationMatches,
    OutputExtracted,
    RegionPresent,
    TextPresent,
    ValidationMessagePresent,
    ValueSet,
    Visible,
)
from cua.surface.evaluators import WebEvaluator
from cua.surface.features import FEATURES, SurfaceDescriptor, SurfaceFeature
from cua.surface.locators import (
    Ambiguous,
    BBox,
    Ladder,
    LadderOutcome,
    NearText,
    Resolved,
    RoleName,
    TableCell,
    Unresolved,
    Within,
    ladder_for,
    resolve_ladder,
)
from cua.surface.protocol import (
    Action,
    ActionFailed,
    ActionResult,
    Click,
    ConditionEvaluator,
    ConditionTimeout,
    DialogEvent,
    ExpectDialog,
    FrameInfo,
    Navigate,
    Node,
    Observation,
    PerceptionDrift,
    Press,
    ReadText,
    RecordingEnv,
    Rect,
    SelectOption,
    SessionHandle,
    StaleRefError,
    Surface,
    SurfaceConfig,
    SurfaceError,
    TypeText,
    Viewport,
)

SUPPORTED_SURFACES: frozenset[str] = frozenset(ADAPTERS)
"""The ``target.surface`` kinds this build can actually drive: the keys of
``adapters.ADAPTERS``. A new adapter joins there, and nothing above the surface
layer changes."""

__all__ = [
    "ADAPTERS",
    "FEATURES",
    "PLAYWRIGHT",
    "SUPPORTED_SURFACES",
    "Action",
    "ActionFailed",
    "ActionResult",
    "AllOf",
    "Ambiguous",
    "AnyOf",
    "BBox",
    "Click",
    "Condition",
    "ConditionEvaluator",
    "ConditionTimeout",
    "DialogEvent",
    "DialogRaised",
    "ErrorBannerPresent",
    "ExpectDialog",
    "FrameInfo",
    "Ladder",
    "LadderOutcome",
    "LocationMatches",
    "Navigate",
    "NearText",
    "Node",
    "Observation",
    "OutputExtracted",
    "PerceptionDrift",
    "Press",
    "ReadText",
    "RecordingEnv",
    "Rect",
    "RegionPresent",
    "Resolved",
    "RoleName",
    "SelectOption",
    "SessionHandle",
    "StaleRefError",
    "Surface",
    "SurfaceConfig",
    "SurfaceDescriptor",
    "SurfaceError",
    "SurfaceFeature",
    "TableCell",
    "TextPresent",
    "TypeText",
    "Unresolved",
    "ValidationMessagePresent",
    "ValueSet",
    "Viewport",
    "Visible",
    "WebEvaluator",
    "Within",
    "adapter_for",
    "ladder_for",
    "resolve_ladder",
]
