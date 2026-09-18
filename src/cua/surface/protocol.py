"""The surface-neutral vocabulary every other layer speaks.

Nothing above this module is allowed to know that the target is a web page.
The agent loop, the replay engine and the capability artifact all deal in
``Observation`` / ``Node`` / ``Action`` / ``Condition`` and let a ``Surface``
implementation decide what those mean for one kind of target. Today there is
one implementation (Playwright, in ``playwright_surface.py``); a desktop
adapter would implement the same protocol and reinterpret the same conditions
(``conditions.py`` carries the per-condition desktop notes).

Two rules the rest of the design leans on:

* **Refs are observation-scoped and never reused.** ``n7`` names one node of
  one observation, and the next observation numbers on from where that one
  stopped. A ref that has outlived its screen therefore addresses nothing and
  raises, instead of addressing whatever has since taken seventh place. The
  alternative — long-lived element handles — silently survives a re-render and
  clicks the wrong thing.
* **No CSS, no XPath.** Controls are addressed through the accessibility tree
  (role, name, structure, geometry), because that is what survives a legacy
  app's regenerated markup. The single document-level exception is the ``body``
  anchor ``aria_snapshot`` needs, and it never names a control.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # locators and conditions are built on these types
    from cua.surface.conditions import Condition
    from cua.surface.locators import Ladder, LadderOutcome

TOP_FRAME = ""
"""Name of the top document. A legacy frameset names its child frames; the
document holding the ``<frameset>`` itself has no name."""

CONTROL_ROLES: frozenset[str] = frozenset(
    {"textbox", "searchbox", "combobox", "listbox", "spinbutton", "checkbox", "radio", "slider"}
)
"""Roles that carry a value a user can set."""

INTERACTIVE_ROLES: frozenset[str] = CONTROL_ROLES | frozenset(
    {"button", "link", "menuitem", "menuitemcheckbox", "tab", "option"}
)

ANCHOR_ROLES: frozenset[str] = frozenset(
    {
        "text",
        "paragraph",
        "cell",
        "columnheader",
        "rowheader",
        "heading",
        "label",
        "listitem",
        "caption",
        "strong",
        "emphasis",
    }
)
"""Roles whose text may anchor a ``near_text`` locator.

Containers (``row``, ``table``) are excluded deliberately: their accessible
name is everything inside them concatenated, so anchoring on one would let
"User ID" match a whole row and put the anchor box in the wrong place.
"""

CONTAINER_ROLES: frozenset[str] = frozenset({"table", "rowgroup", "row", "list", "form", "group"})


class Rect(BaseModel):
    """A box in page coordinates.

    Coordinates are relative to the top-level viewport even for nodes inside a
    frame, which is what lets a masked screenshot and a ``bbox`` locator agree
    with each other on a frameset page.
    """

    model_config = ConfigDict(frozen=True)

    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)

    @property
    def empty(self) -> bool:
        return self.w <= 0 or self.h <= 0

    def contains_center_of(self, other: Rect) -> bool:
        cx, cy = other.center
        return self.x <= cx <= self.right and self.y <= cy <= self.bottom


class Viewport(BaseModel):
    model_config = ConfigDict(frozen=True)

    w: int
    h: int


class SurfaceConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    near_text_max_px: float = 320.0
    near_text_tolerance_px: float = 6.0
    validation_proximity_px: float = 200.0


class RecordingEnv(BaseModel):
    """The environment a capability was recorded in.

    Pixel-addressed locators mean nothing outside it, so ``resolve_ladder``
    refuses a ``bbox`` rung anywhere else. Carried in the artifact as
    ``recording_env``.
    """

    model_config = ConfigDict(frozen=True)

    viewport: Viewport
    dpr: float = 1.0


class Node(BaseModel):
    """One node of the compact accessibility tree.

    ``name`` is the accessible name, which on this class of app is very often
    empty — the whole reason the locator ladder has rungs below ``role_name``.
    ``near_text`` records the label text a human reads as belonging to the
    control, which is how an unnamed textbox stays describable to a model and
    to a reviewer.
    """

    model_config = ConfigDict(frozen=True)

    ref: str
    role: str
    name: str = ""
    value: str | None = None
    frame: str = TOP_FRAME
    bbox: Rect | None = None
    near_text: str | None = None
    parent: str | None = None
    ordinal: int = 0
    """Index among the nodes of this role in this frame. It is how a ref
    becomes something clickable without a CSS selector: the n-th ``textbox`` of
    frame ``main``. Role-only rather than role-and-name, because the controls
    this system exists for have no name to count by."""
    attrs: dict[str, str] = Field(default_factory=dict)

    @property
    def interactive(self) -> bool:
        return self.role in INTERACTIVE_ROLES

    @property
    def text(self) -> str:
        """What a human reads off this node: its value if it has one, else its name."""
        return self.value if self.value else self.name

    @property
    def label(self) -> str:
        """Short human description, for logs and failure messages."""
        named = f'{self.role} "{self.name}"' if self.name else self.role
        if self.name or self.near_text is None:
            return named
        return f"{named} (near {self.near_text!r})"


class FrameInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    url: str
    status: int | None = None
    """HTTP status of the document response that produced this frame, when the
    surface saw the navigation. ``error_banner_present`` leans on it, because a
    legacy 500 page has no ARIA to give itself away."""


class Observation(BaseModel):
    """Everything one look at the target produced.

    Deliberately a plain data object with no live handles: an observation can
    be written to the evidence log, read back a week later, and re-evaluated by
    the same condition and locator code that ran against the live browser. That
    property is what lets the replay engine's detectors be pure functions.
    """

    model_config = ConfigDict(frozen=True)

    observed_at: datetime
    location: str
    """Top document location. On a frameset that is the shell URL, so
    ``location_matches`` looks at every frame — see ``conditions.py``."""
    title: str
    viewport: Viewport
    frames: list[FrameInfo] = Field(default_factory=list)
    nodes: list[Node] = Field(default_factory=list)
    screenshot_png: bytes | None = Field(default=None, exclude=True, repr=False)
    """Masked PNG, excluded from serialization: evidence stores it as a file
    beside the JSONL record, never inline."""

    def node(self, ref: str) -> Node:
        found = self.find(ref)
        if found is None:
            raise StaleRefError(f"no node {ref!r} in this observation")
        return found

    def find(self, ref: str) -> Node | None:
        return next((n for n in self.nodes if n.ref == ref), None)

    def children(self, ref: str) -> list[Node]:
        return [n for n in self.nodes if n.parent == ref]

    def descendants(self, ref: str) -> Iterator[Node]:
        for child in self.children(ref):
            yield child
            yield from self.descendants(child.ref)

    def in_frame(self, frame: str | None) -> list[Node]:
        if frame is None:
            return list(self.nodes)
        return [n for n in self.nodes if n.frame == frame]

    def frame_info(self, name: str) -> FrameInfo | None:
        return next((f for f in self.frames if f.name == name), None)

    def compact(self) -> str:
        """Render the tree the way the agent loop and a human reviewer read it.

        Containers are dropped and table rows are flattened onto one line. What
        survives is what a decision can be made from: controls, headings, table
        contents, standalone text — each keeping its ref.
        """
        return _compact(self)


class SessionHandle(BaseModel):
    """What a live session exposes to whoever may need to take it over.

    Handoff (M6) is only credible if a second process can attach to the very
    browser the automation is driving, so the CDP endpoint belongs in the
    surface contract rather than in the Playwright implementation's details.
    """

    model_config = ConfigDict(frozen=True)

    cdp_url: str
    page_url: str
    viewport: Viewport


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


class Click(BaseModel):
    model_config = ConfigDict(frozen=True)
    action: Literal["click"] = "click"
    ref: str


class TypeText(BaseModel):
    model_config = ConfigDict(frozen=True)
    action: Literal["type"] = "type"
    ref: str
    text: str
    clear: bool = True


class Press(BaseModel):
    model_config = ConfigDict(frozen=True)
    action: Literal["press"] = "press"
    key: str
    ref: str | None = None


class SelectOption(BaseModel):
    model_config = ConfigDict(frozen=True)
    action: Literal["select"] = "select"
    ref: str
    value: str


class ReadText(BaseModel):
    model_config = ConfigDict(frozen=True)
    action: Literal["read"] = "read"
    ref: str


class Navigate(BaseModel):
    model_config = ConfigDict(frozen=True)
    action: Literal["navigate"] = "navigate"
    url: str


class Hover(BaseModel):
    model_config = ConfigDict(frozen=True)
    action: Literal["hover"] = "hover"
    ref: str


class Drag(BaseModel):
    model_config = ConfigDict(frozen=True)
    action: Literal["drag"] = "drag"
    source_ref: str
    target_ref: str


Action = Annotated[
    Click | TypeText | Press | SelectOption | ReadText | Navigate | Hover | Drag,
    Field(discriminator="action"),
]


class ActionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: str
    ref: str | None = None
    text: str | None = None
    """What ``read`` returned."""
    duration_ms: int = 0


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class SurfaceError(Exception):
    """Base for every fault the surface reports upward."""


class StaleRefError(SurfaceError):
    """A ref was used against an observation other than the one that minted it.

    Refs are not reused between observations, so a ref that has outlived its
    screen cannot quietly address a different node: it addresses nothing.
    """


class PerceptionDrift(SurfaceError):
    """A ref no longer points at the node it was recorded against.

    Raised instead of acting. The ref-to-element mapping is re-checked against
    the live accessibility tree immediately before every action, so a page that
    re-rendered between observing and acting produces a fault rather than a
    click on whatever moved into that position.
    """


class ConditionTimeout(SurfaceError):
    """``wait_for`` gave up.

    Carries the last observation so the replay engine can fill the
    ``expected``/``observed`` pair its failure contract promises.
    """

    def __init__(self, condition: Condition, timeout_s: float, observation: Observation) -> None:
        super().__init__(f"condition still false after {timeout_s}s: {condition!r}")
        self.condition = condition
        self.timeout_s = timeout_s
        self.observation = observation


class ActionFailed(SurfaceError):
    """The action reached the right node and the target refused it."""


# --------------------------------------------------------------------------
# The protocol
# --------------------------------------------------------------------------


class Surface(Protocol):
    """The only way anything above this package touches a target."""

    def observe(self, *, screenshot: bool = False, masks: Sequence[Ladder] = ()) -> Observation:
        """Look at the target.

        ``masks`` are locator ladders painted out before the screenshot is
        taken, never cropped afterwards — a redaction that runs after capture
        has already put the secret in memory.
        """
        ...

    def act(self, action: Action) -> ActionResult:
        """Perform one action, against refs from the current observation."""
        ...

    def resolve(self, ladder: Ladder, *, observation: Observation | None = None) -> LadderOutcome:
        """Walk a locator ladder: which rung answered, and with how many
        matches. Never picks between matches."""
        ...

    def wait_for(self, condition: Condition, timeout_s: float) -> Observation:
        """Observe until the condition holds. Raises ``ConditionTimeout``."""
        ...

    def evaluate(self, condition: Condition) -> bool:
        """Evaluate a condition against a fresh observation."""
        ...

    def expose(self) -> SessionHandle:
        """Publish the live session so a human can be handed the controls."""
        ...


# --------------------------------------------------------------------------
# Compact rendering
# --------------------------------------------------------------------------

_SKIP_IN_COMPACT: frozenset[str] = frozenset({"rowgroup", "generic", "none", "presentation"})


def _node_line(node: Node) -> str:
    bits = [node.ref, node.role]
    if node.name:
        bits.append(f'"{node.name}"')
    if node.value:
        bits.append(f"={node.value!r}")
    if not node.name and node.near_text:
        bits.append(f"~{node.near_text!r}")
    return " ".join(bits)


def _cell_contents(obs: Observation, cell: Node) -> list[Node]:
    """What a table cell is worth printing as.

    A legacy layout wraps every control in a cell, so a cell holding a control
    should read as that control — printing both the wrapper and its contents
    doubles the tree the model has to read without adding a fact to it.
    """
    inner = [
        n
        for n in obs.descendants(cell.ref)
        if n.interactive and not _has_interactive_ancestor(obs, n, stop=cell.ref)
    ]
    if inner:
        return inner
    return [cell] if cell.name else []


def _has_interactive_ancestor(obs: Observation, node: Node, *, stop: str) -> bool:
    """Is this node inside another control? A combobox's options are the
    combobox's business; the row only needs to name the combobox."""
    parent = node.parent
    while parent is not None and parent != stop:
        found = obs.find(parent)
        if found is None:
            return False
        if found.interactive:
            return True
        parent = found.parent
    return False


def _compact_frame(obs: Observation, frame: str, out: list[str]) -> None:
    info = obs.frame_info(frame)
    out.append(f"[{frame or 'top'}] {info.url if info else ''}".rstrip())
    rendered: set[str] = set()

    for node in obs.in_frame(frame):
        if node.ref in rendered or node.role in _SKIP_IN_COMPACT:
            continue
        if node.role == "row":
            cells = [c for c in obs.descendants(node.ref) if c.role in ("cell", "columnheader")]
            shown = [n for cell in cells for n in _cell_contents(obs, cell)]
            if shown:
                out.append("  row: " + " | ".join(_node_line(n) for n in shown))
            rendered.add(node.ref)
            rendered.update(n.ref for n in obs.descendants(node.ref))
            continue
        if node.role in CONTAINER_ROLES:
            continue
        out.append("  " + _node_line(node))
        rendered.add(node.ref)


def _compact(obs: Observation) -> str:
    out: list[str] = []
    for frame in dict.fromkeys(n.frame for n in obs.nodes):
        _compact_frame(obs, frame, out)
    return "\n".join(out)
