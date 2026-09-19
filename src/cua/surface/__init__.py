"""Perception and action, one step removed from any particular UI technology.

``protocol`` holds the vocabulary, ``a11y`` turns an accessibility snapshot
into it, ``locators`` finds a control four increasingly desperate ways,
``conditions`` says what a step is waiting for, ``evaluators`` says what that
means on the web, and ``playwright_surface`` is the one implementation that
touches a browser.
"""

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

SUPPORTED_SURFACES: frozenset[str] = frozenset({"web", "legacy_web"})
"""The ``target.surface`` kinds this build can actually drive.

Both are driven by ``PlaywrightSurface``: a legacy web app is still a web page,
frameset and all. ``desktop`` is the declared but unbuilt case — it needs a
``Surface`` over UIA or AX — so a capability that names it is refused before a
browser starts, rather than handed to the wrong adapter. A new adapter joins
the set here, and nothing above the surface layer changes.
"""

__all__ = [
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
    "SurfaceError",
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
    "ladder_for",
    "resolve_ladder",
]
