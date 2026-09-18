"""Perception: an accessibility snapshot becomes a flat, ref-addressed tree.

Playwright renders an ARIA snapshot as YAML::

    - heading "Member Search" [level=3]
    - table:
      - rowgroup:
        - row "Member ID Search":
          - cell "Member ID"
          - cell:
            - textbox
          - cell "Search":
            - button "Search"

That is the whole perception input. ``page.accessibility.snapshot()`` is
deprecated, and a frameset has no single tree, so the surface takes one
snapshot per frame and this module stitches the results into one observation.

Two decisions worth defending:

* **YAML is parsed with PyYAML, not a hand-written reader.** Playwright emits
  YAML deliberately (its ``toMatchAriaSnapshot`` assertions are YAML), so its
  quoting and escaping rules are YAML's. Reimplementing them would be a second
  grammar to keep in sync. The one thing PyYAML does that would hurt is
  coercing plain scalars — a cell reading ``No`` becoming ``False``, a member
  id becoming an int — so every scalar is loaded as text (``_TextLoader``).
* **Nodes keep their tree.** ``table_cell`` needs rows and columns, and
  ``near_text`` needs to exclude container names, so parents are preserved
  rather than flattened away.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from cua.surface.protocol import (
    ANCHOR_ROLES,
    CONTROL_ROLES,
    DEFAULT_CONFIG,
    TOP_FRAME,
    Node,
    Rect,
    SurfaceConfig,
)


class _TextLoader(yaml.SafeLoader):
    """SafeLoader that leaves plain scalars as the text they were.

    YAML 1.1 turns ``No``, ``Off`` and ``12345`` into a bool, a bool and an
    int. A legacy screen is full of exactly those strings, and a member id that
    silently becomes an integer would break every name comparison downstream.
    """


def _as_text(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    assert isinstance(node, yaml.ScalarNode)
    return str(loader.construct_scalar(node))


for _tag in (
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:timestamp",
    "tag:yaml.org,2002:null",
):
    _TextLoader.add_constructor(_tag, _as_text)


# ``role "name" [attr=value] [attr]`` — the header Playwright writes for a node.
_HEADER = re.compile(
    r"""^(?P<role>[a-zA-Z/][\w/-]*)          # role, or /url for a link's href
        (?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?  # optional quoted accessible name
        (?P<attrs>(?:\s*\[[^\]]*\])*)\s*$    # optional [level=3] [selected]
    """,
    re.VERBOSE,
)
_ATTR = re.compile(r"\[([^\]=]+)(?:=([^\]]*))?\]")

# Sub-entries that describe the parent rather than being nodes of their own.
_PROPERTY_KEYS = frozenset({"/url"})


class SnapshotParseError(ValueError):
    """The snapshot was not the YAML Playwright promises."""


class ParsedNode:
    """A node as it came out of the snapshot, before refs and geometry.

    Not a pydantic model: this is the intermediate form, and keeping it plain
    keeps the parser readable and cheap.
    """

    __slots__ = ("attrs", "children", "name", "role", "value")

    def __init__(
        self,
        role: str,
        name: str = "",
        value: str | None = None,
        attrs: dict[str, str] | None = None,
    ) -> None:
        self.role = role
        self.name = name
        self.value = value
        self.attrs: dict[str, str] = attrs or {}
        self.children: list[ParsedNode] = []

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ParsedNode({self.role!r}, {self.name!r}, value={self.value!r})"


def parse_snapshot(text: str) -> list[ParsedNode]:
    """Parse one frame's ARIA snapshot into a forest of ``ParsedNode``."""
    if not text.strip():
        return []
    try:
        loaded = yaml.load(text, Loader=_TextLoader)
    except yaml.YAMLError as exc:  # pragma: no cover - Playwright emits valid YAML
        raise SnapshotParseError(str(exc)) from exc
    if loaded is None:
        return []
    if not isinstance(loaded, list):
        raise SnapshotParseError(f"expected a list at the top level, got {type(loaded).__name__}")
    return _parse_items(loaded)


def _parse_items(items: list[Any]) -> list[ParsedNode]:
    out: list[ParsedNode] = []
    for item in items:
        node = _parse_item(item)
        if node is not None:
            out.append(node)
    return out


def _parse_item(item: Any) -> ParsedNode | None:
    """One YAML list entry: a bare role, or a single-key mapping."""
    if isinstance(item, str):
        return _from_header(item)

    if isinstance(item, dict):
        if len(item) != 1:  # pragma: no cover - Playwright emits one key per entry
            raise SnapshotParseError(f"expected a single-key mapping, got {sorted(item)}")
        ((header, body),) = item.items()
        if header in _PROPERTY_KEYS:
            return None
        node = _from_header(str(header))
        if node is None:
            return None
        if isinstance(body, list):
            for child in _parse_items(body):
                node.children.append(child)
            _absorb_properties(node, body)
        elif body is not None:
            _set_scalar(node, str(body))
        return node

    raise SnapshotParseError(f"unexpected snapshot entry: {item!r}")


def _absorb_properties(node: ParsedNode, body: list[Any]) -> None:
    for item in body:
        if isinstance(item, dict) and len(item) == 1:
            ((key, value),) = item.items()
            if key in _PROPERTY_KEYS and value is not None:
                node.attrs[str(key).lstrip("/")] = str(value)


def _set_scalar(node: ParsedNode, scalar: str) -> None:
    """A scalar body is a value on a control and the text itself anywhere else.

    ``- textbox: operator`` is what the operator typed; ``- text: Functions``
    is the text. Conflating the two would make ``value_set`` true for every
    paragraph on the screen.
    """
    if node.role in CONTROL_ROLES:
        node.value = scalar
    elif not node.name:
        node.name = scalar


def _from_header(header: str) -> ParsedNode | None:
    match = _HEADER.match(header.strip())
    if match is None:
        raise SnapshotParseError(f"unparsable snapshot header: {header!r}")
    role = match.group("role")
    if role in _PROPERTY_KEYS or role.startswith("/"):
        return None
    name = _unescape(match.group("name") or "")
    # ``[level=3]`` has a value; ``[selected]`` is the value by being there.
    attrs = {k.strip(): (v.strip() or "true") for k, v in _ATTR.findall(match.group("attrs") or "")}
    return ParsedNode(role=role, name=name, attrs=attrs)


def _unescape(text: str) -> str:
    return text.replace('\\"', '"').replace("\\\\", "\\")


# --------------------------------------------------------------------------
# Flattening: refs, ordinals, values
# --------------------------------------------------------------------------


def flatten(
    roots: list[ParsedNode],
    *,
    frame: str = TOP_FRAME,
    start_index: int = 1,
) -> list[Node]:
    """Turn a parsed forest into ``Node``s with refs, parents and ordinals.

    Refs are assigned in document order across the whole observation, which is
    why ``start_index`` is passed in: the second frame continues the first
    frame's numbering instead of minting a second ``n1``.

    ``ordinal`` counts nodes of the same role within the frame. It has to be
    role-only rather than role-and-name: an unnamed textbox cannot be asked for
    by name, and a role query returns named and unnamed elements alike, so this
    is the index that addresses either one.
    """
    out: list[Node] = []
    counter = start_index
    seen: dict[str, int] = {}

    def walk(node: ParsedNode, parent: str | None) -> None:
        nonlocal counter
        ref = f"n{counter}"
        counter += 1
        ordinal = seen.get(node.role, 0)
        seen[node.role] = ordinal + 1
        out.append(
            Node(
                ref=ref,
                role=node.role,
                name=node.name,
                value=_value_of(node),
                frame=frame,
                parent=parent,
                ordinal=ordinal,
                attrs=dict(node.attrs),
            )
        )
        for child in node.children:
            walk(child, ref)

    for root in roots:
        walk(root, None)
    return out


def _value_of(node: ParsedNode) -> str | None:
    """A control's current value.

    A ``<select>`` renders as a combobox whose selected option is a child, so
    its value has to be read off that child rather than off the combobox.
    """
    if node.value is not None:
        return node.value
    if node.role in ("combobox", "listbox"):
        for child in node.children:
            if child.role == "option" and child.attrs.get("selected"):
                return child.name
    return None


def with_geometry(
    nodes: list[Node],
    boxes: dict[str, Rect | None],
    config: SurfaceConfig = DEFAULT_CONFIG,
) -> list[Node]:
    """Attach measured boxes, then label the controls that have no name."""
    placed = [n.model_copy(update={"bbox": boxes.get(n.ref)}) for n in nodes]
    return annotate_near_text(placed, config)


def annotate_near_text(nodes: list[Node], config: SurfaceConfig = DEFAULT_CONFIG) -> list[Node]:
    """Record, for each unnamed control, the text a human would read as its label.

    Legacy forms label a field with a plain table cell to its left (or a line
    above it), never with ``<label for>``. Recovering that association is what
    makes an unnamed textbox describable in the compact tree, and it is the
    same geometry the ``near_text`` locator rung uses at replay time.
    """
    anchors = [n for n in nodes if n.role in ANCHOR_ROLES and n.name and n.bbox]
    out: list[Node] = []
    for node in nodes:
        if node.name or not node.interactive or node.bbox is None:
            out.append(node)
            continue
        label = _nearest_label(node, [a for a in anchors if a.frame == node.frame], config)
        out.append(node.model_copy(update={"near_text": label}))
    return out


def _nearest_label(node: Node, anchors: list[Node], config: SurfaceConfig) -> str | None:
    assert node.bbox is not None
    best: tuple[float, str] | None = None
    for anchor in anchors:
        assert anchor.bbox is not None
        distance = _label_distance(anchor.bbox, node.bbox, config)
        if distance is None:
            continue
        if best is None or distance < best[0]:
            best = (distance, anchor.name)
    return best[1] if best else None


def _label_distance(anchor: Rect, control: Rect, config: SurfaceConfig) -> float | None:
    """Distance from a label to the control it plausibly labels, or None.

    Only two arrangements count, because only two are conventional: the label
    to the left on the same line, and the label directly above. Anything else
    is a coincidence of layout, and adopting it would invent a relationship the
    page does not express.
    """
    tol, far = config.near_text_tolerance_px, config.near_text_max_px
    if shares_row(anchor, control, config) and control.x >= anchor.right - tol:
        gap = control.x - anchor.right
        return gap if gap <= far else None
    if shares_column(anchor, control, config) and control.y >= anchor.bottom - tol:
        gap = control.y - anchor.bottom
        # A label above is the weaker convention; bias against it so a cell on
        # the same line always wins.
        return gap + far if gap <= far else None
    return None


def shares_row(a: Rect, b: Rect, config: SurfaceConfig) -> bool:
    """Are these two boxes on the same line of the screen?

    Centre-in-span rather than plain rectangle overlap: on a dense legacy form
    the next row starts three pixels below this one, and overlap-with-tolerance
    would call two stacked fields neighbours on the same line.
    """
    tol = config.near_text_tolerance_px
    return a.y - tol <= b.center[1] <= a.bottom + tol or b.y - tol <= a.center[1] <= b.bottom + tol


def shares_column(a: Rect, b: Rect, config: SurfaceConfig) -> bool:
    """Are these two boxes in the same column of the screen?"""
    tol = config.near_text_tolerance_px
    return a.x - tol <= b.center[0] <= a.right + tol or b.x - tol <= a.center[0] <= b.right + tol


def normalize(text: str) -> str:
    """Whitespace-collapsed, case-folded text, for every name comparison.

    A legacy template wraps and indents its labels; the same label arrives as
    ``"User ID"`` on one screen and ``"User  ID"`` on another.
    """
    return " ".join(text.split()).casefold()
