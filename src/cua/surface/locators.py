"""The locator ladder: four ways to name a control, tried in order.

A recorded step does not store "the element at ``table > tr:nth-child(2) > td
> input``". It stores a ladder of strategies, strongest first, and every rung
must resolve to **exactly one** node or it is not used. That rule is the whole
point:

* A rung that matches nothing falls through to the next rung. If every rung
  falls through, the capability reports ``LOCATOR_UNRESOLVED`` — a signal that
  the app changed, not a wrong click.
* A rung that matches *more than one* node also falls through, and the fact is
  recorded. A ladder that silently picked the first of two "Search" buttons
  would be right most of the time and catastrophically wrong occasionally;
  falling through to a rung that can tell them apart is the only honest move.
* Which rung answered is recorded on every run (``locator_rungs_used``). A step
  that used to resolve by ``role_name`` and now resolves by ``near_text`` is
  drift worth a warning, even though the run succeeded.

The rungs, strongest to weakest:

``role_name``   the control's role and accessible name — survives layout changes
``near_text``   the label a human reads next to it — survives renaming the control
``table_cell``  row and column of a grid — how a legacy app's data is addressed
``bbox``        pixels, valid only in the viewport the capability was recorded in

Every rung is a pure function of an ``Observation``, so a ladder can be
re-resolved offline against a recorded observation — which is how a rung slip
is explained after the fact without re-running the app.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from cua.surface.a11y import (
    normalize,
    shares_column,
    shares_row,
)
from cua.surface.protocol import (
    ANCHOR_ROLES,
    CONTAINER_ROLES,
    Node,
    Observation,
    RecordingEnv,
    Rect,
    SurfaceConfig,
)

# The rung and the ``near_text`` annotation on a node have to agree about what
# "beside" means, or the compact tree would describe a control by a label that
# cannot then find it. Both use the geometry defined in ``a11y``.

Direction: TypeAlias = Literal["right", "below", "left", "above"]

DEFAULT_DIRECTIONS: tuple[Direction, ...] = ("right", "below")
"""Where a labelled control sits when the artifact does not say. Reading order
for a left-to-right form: the field is to the right of its label, or under it.
The first direction that finds *any* candidate decides — we never widen the
search after a hit, because a second direction can only add ambiguity."""


class Within(BaseModel):
    """Scope for a rung. ``frame`` is a frameset's frame name."""

    model_config = ConfigDict(frozen=True)

    frame: str | None = None


class RoleName(BaseModel):
    """Role plus accessible name: the rung to prefer when the app gives a name."""

    model_config = ConfigDict(frozen=True)

    strategy: Literal["role_name"] = "role_name"
    role: str
    name: str
    exact: bool = True
    within: Within | None = None


class NearText(BaseModel):
    """The control next to a piece of text.

    The rung that earns its keep on a legacy app: a textbox with no ``<label
    for>`` has no accessible name at all, so the only durable way to say which
    box to type in is "the one beside the words *Member ID*".
    """

    model_config = ConfigDict(frozen=True)

    strategy: Literal["near_text"] = "near_text"
    text: str
    role: str | None = None
    direction: Direction | None = None
    exact: bool = False
    within: Within | None = None


class TableCell(BaseModel):
    """A cell addressed by its row's content and its column's header.

    How a human reads a legacy grid ("the Balance column of the Savings row"),
    and stable under a column being inserted, which a pixel or an index is not.
    """

    model_config = ConfigDict(frozen=True)

    strategy: Literal["table_cell"] = "table_cell"
    row_contains: str
    column_header: str
    exact: bool = False
    within: Within | None = None


class BBox(BaseModel):
    """Pixels. The rung of last resort.

    Valid only in the viewport the capability was recorded in, which is why
    ``recording_env`` exists and why ``resolve_ladder`` refuses this rung
    anywhere else rather than clicking approximately the right place.
    """

    model_config = ConfigDict(frozen=True)

    strategy: Literal["bbox"] = "bbox"
    x: float
    y: float
    w: float
    h: float
    role: str | None = None
    confidence: Literal["low", "medium", "high"] = "low"
    within: Within | None = None

    def rect(self) -> Rect:
        return Rect(x=self.x, y=self.y, w=self.w, h=self.h)


LocatorStrategy = Annotated[
    RoleName | NearText | TableCell | BBox,
    Field(discriminator="strategy"),
]

Ladder: TypeAlias = list[RoleName | NearText | TableCell | BBox]


class RungAttempt(BaseModel):
    """What one rung did. Kept for every rung, including the ones that failed:
    a failure report that says only "not found" cannot be debugged."""

    model_config = ConfigDict(frozen=True)

    rung: str
    matches: int = 0
    refs: list[str] = Field(default_factory=list)
    refused: str | None = None
    """Set when the rung was never tried, with the reason."""


class Resolved(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["resolved"] = "resolved"
    node: Node
    rung: str
    rung_index: int
    attempts: list[RungAttempt] = Field(default_factory=list)

    @property
    def ref(self) -> str:
        return self.node.ref

    @property
    def slipped(self) -> bool:
        """True when a stronger rung was tried and did not answer.

        Not a failure — the run continues on the rung that did answer — but it
        means the app no longer looks the way it did when this capability was
        recorded, and the run says so.
        """
        return self.rung_index > 0


class Ambiguous(BaseModel):
    """Every rung either missed or matched several nodes, and at least one
    matched several. Distinct from ``Unresolved`` because the fixes differ: an
    ambiguous ladder needs a narrower rung, an unresolved one needs a new one."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["ambiguous"] = "ambiguous"
    attempts: list[RungAttempt] = Field(default_factory=list)

    @property
    def matches(self) -> int:
        return max((a.matches for a in self.attempts), default=0)


class Unresolved(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["unresolved"] = "unresolved"
    attempts: list[RungAttempt] = Field(default_factory=list)


LadderOutcome: TypeAlias = Resolved | Ambiguous | Unresolved


def resolve_ladder(
    ladder: Ladder,
    observation: Observation,
    *,
    recording_env: RecordingEnv | None = None,
    config: SurfaceConfig = SurfaceConfig(),
) -> LadderOutcome:
    """Try each rung in order; the first to match exactly one node wins."""
    attempts: list[RungAttempt] = []

    for index, strategy in enumerate(ladder):
        refusal = _refusal(strategy, observation, recording_env)
        if refusal is not None:
            attempts.append(RungAttempt(rung=strategy.strategy, refused=refusal))
            continue

        matches = match(strategy, observation, config=config)
        attempts.append(
            RungAttempt(
                rung=strategy.strategy,
                matches=len(matches),
                refs=[n.ref for n in matches],
            )
        )
        if len(matches) == 1:
            return Resolved(
                node=matches[0],
                rung=strategy.strategy,
                rung_index=index,
                attempts=attempts,
            )

    if any(a.matches > 1 for a in attempts):
        return Ambiguous(attempts=attempts)
    return Unresolved(attempts=attempts)


def _refusal(
    strategy: RoleName | NearText | TableCell | BBox,
    observation: Observation,
    recording_env: RecordingEnv | None,
) -> str | None:
    """Reasons to skip a rung without even looking at the tree."""
    if not isinstance(strategy, BBox):
        return None
    if recording_env is None:
        return "no recording environment: pixel locators are not portable"
    if recording_env.viewport != observation.viewport:
        return (
            f"viewport {observation.viewport.w}x{observation.viewport.h} "
            f"differs from the recorded {recording_env.viewport.w}x{recording_env.viewport.h}"
        )
    return None


def match(
    strategy: RoleName | NearText | TableCell | BBox,
    observation: Observation,
    config: SurfaceConfig = SurfaceConfig(),
) -> list[Node]:
    """Every node one rung matches. The ladder, not the rung, decides what to
    do with a count other than one."""
    if isinstance(strategy, RoleName):
        return _match_role_name(strategy, observation)
    if isinstance(strategy, NearText):
        return _match_near_text(strategy, observation, config)
    if isinstance(strategy, TableCell):
        return _match_table_cell(strategy, observation)
    return _match_bbox(strategy, observation)


def _scope(observation: Observation, within: Within | None) -> list[Node]:
    if within is None or within.frame is None:
        return list(observation.nodes)
    return observation.in_frame(within.frame)


def _match_role_name(strategy: RoleName, observation: Observation) -> list[Node]:
    wanted = normalize(strategy.name)
    out: list[Node] = []
    for node in _scope(observation, strategy.within):
        if node.role != strategy.role:
            continue
        name = normalize(node.name)
        hit = name == wanted if strategy.exact else wanted in name
        if hit:
            out.append(node)
    return out


def _match_near_text(strategy: NearText, observation: Observation, config: SurfaceConfig) -> list[Node]:
    wanted = normalize(strategy.text)
    anchors = [
        n
        for n in _scope(observation, strategy.within)
        if n.role in ANCHOR_ROLES and n.bbox is not None and (normalize(n.name) == wanted if strategy.exact else wanted in normalize(n.name))
    ]
    if not anchors:
        return []

    directions = (strategy.direction,) if strategy.direction else DEFAULT_DIRECTIONS
    for direction in directions:
        found: dict[str, Node] = {}
        for anchor in anchors:
            for candidate in _candidates_for(strategy, observation, anchor):
                if _distance(anchor, candidate, direction, config) is not None:
                    found[candidate.ref] = candidate
        if found:
            return [n for n in observation.nodes if n.ref in found]
    return []


def _candidates_for(strategy: NearText, observation: Observation, anchor: Node) -> list[Node]:
    """What may be found by a label.

    Same frame as the anchor: coordinates are comparable across frames, but a
    label in the nav pane does not label a field in the main pane, and treating
    it as if it did is how the ``ambiguous_button`` case would go wrong.

    With no role given, only interactive nodes are eligible, so "the text to
    the right of a label" is never mistaken for a control.
    """
    out: list[Node] = []
    for node in observation.in_frame(anchor.frame):
        if node.ref == anchor.ref or node.bbox is None:
            continue
        if strategy.role is None:
            if not node.interactive:
                continue
        elif node.role != strategy.role:
            continue
        out.append(node)
    return out


def _distance(anchor: Node, candidate: Node, direction: Direction, config: SurfaceConfig) -> float | None:
    """Gap between a label and a candidate in one direction, or None if the
    candidate is not in that direction, or is too far away to be related."""
    assert anchor.bbox is not None and candidate.bbox is not None
    a, c = anchor.bbox, candidate.bbox

    if direction in ("right", "left"):
        if not shares_row(a, c, config):
            return None
        gap = c.x - a.right if direction == "right" else a.x - c.right
    else:
        if not shares_column(a, c, config):
            return None
        gap = c.y - a.bottom if direction == "below" else a.y - c.bottom

    if gap < -config.near_text_tolerance_px or gap > config.near_text_max_px:
        return None
    return gap


def _match_table_cell(strategy: TableCell, observation: Observation) -> list[Node]:
    wanted_row = normalize(strategy.row_contains)
    wanted_col = normalize(strategy.column_header)
    out: list[Node] = []

    for table in _scope(observation, strategy.within):
        if table.role != "table":
            continue
        rows = [n for n in observation.descendants(table.ref) if n.role == "row"]
        if not rows:
            continue
        header_cells = _cells_of(observation, rows[0])
        column = next(
            (i for i, cell in enumerate(header_cells) if normalize(cell.name) == wanted_col),
            None,
        )
        if column is None:
            continue
        for row in rows[1:]:
            cells = _cells_of(observation, row)
            if column >= len(cells):
                continue
            if any((normalize(cell.name) == wanted_row if strategy.exact else wanted_row in normalize(cell.name)) for cell in cells):
                out.append(cells[column])
    return out


def _cells_of(observation: Observation, row: Node) -> list[Node]:
    return [
        n
        for n in observation.descendants(row.ref)
        if n.role in ("cell", "columnheader", "rowheader")
    ]


def _match_bbox(strategy: BBox, observation: Observation) -> list[Node]:
    rect = strategy.rect()
    candidates = [
        n
        for n in _scope(observation, strategy.within)
        if n.bbox is not None
        and not n.bbox.empty
        and n.role not in CONTAINER_ROLES
        and rect.contains_center_of(n.bbox)
        and (strategy.role is None or n.role == strategy.role)
    ]
    innermost = _innermost(observation, candidates)
    return [innermost] if innermost is not None else candidates


def _innermost(observation: Observation, candidates: list[Node]) -> Node | None:
    """Collapse a containment chain to its innermost node.

    A rect drawn around a textbox also contains the centre of the table cell
    wrapping it. Those are not two answers to "which control" — one is inside
    the other — so the deepest is the answer. Siblings are left alone, and the
    ladder treats them as the ambiguity they are.
    """
    if len(candidates) < 2:
        return candidates[0] if candidates else None
    by_ref = {n.ref: n for n in candidates}
    for node in candidates:
        others = set(by_ref) - {node.ref}
        ancestors = set(_ancestor_refs(observation, node))
        if others <= ancestors:
            return node
    return None


def _ancestor_refs(observation: Observation, node: Node) -> list[str]:
    refs: list[str] = []
    current = node.parent
    while current is not None:
        refs.append(current)
        parent = observation.find(current)
        current = parent.parent if parent else None
    return refs
