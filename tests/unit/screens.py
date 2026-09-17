"""Screens captured from the mock app, and a table layout engine for them.

The snapshots below are verbatim ``aria_snapshot()`` output from the real mock
app, so the parser is tested against what Playwright actually emits rather than
against what this repo wishes it emitted. Re-capture them with::

    python -m cua.surface --url http://127.0.0.1:8000/login

The locator rungs need geometry as well, and a unit test should not need a
browser to get it. ``build`` therefore lays the parsed tree out the way a
browser lays out a table — rows stacked, cells left to right, controls inset
inside their cell. It is a fake, but it is a fake of the one thing the rungs
care about: which box sits beside which. The integration tests run the same
rungs against real measured boxes, so a lie told here is caught there.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cua.surface.a11y import annotate_near_text, flatten, parse_snapshot
from cua.surface.protocol import FrameInfo, Node, Observation, Rect, Viewport

LOGIN = """
- text: MockCore Member Services 1.0
- heading "Sign On" [level=3]
- table:
  - rowgroup:
    - row "User ID":
      - cell "User ID"
      - cell:
        - textbox
    - row "Password":
      - cell "Password"
      - cell:
        - textbox
    - row "Sign On":
      - cell
      - cell "Sign On":
        - button "Sign On"
- paragraph: MockCore is a simulated core banking system. All member data is synthetic.
"""

NAV = """
- text: Functions
- table:
  - rowgroup:
    - row "Member Search":
      - cell "Member Search":
        - link "Member Search":
          - /url: javascript:parent.main.location='/search'
    - row "Account Inquiry":
      - cell "Account Inquiry":
        - link "Account Inquiry":
          - /url: javascript:parent.main.location='/search'
    - row "Sign Off":
      - cell "Sign Off":
        - link "Sign Off":
          - /url: /logoff
"""

NAV_WITH_SECOND_SEARCH = (
    NAV
    + """
- table:
  - rowgroup:
    - row:
      - cell:
        - textbox
    - row "Search":
      - cell "Search":
        - button "Search"
"""
)
"""The ``ambiguous_button`` inject: a second control named "Search", in the
other frame, where a frame-blind ``role_name`` rung will find it."""

SEARCH = """
- text: MockCore Member Services 1.0
- heading "Member Search" [level=3]
- table:
  - rowgroup:
    - row "Member ID Search":
      - cell "Member ID"
      - cell:
        - textbox
      - cell "Search":
        - button "Search"
- paragraph: Enter a five digit member number.
"""

SEARCH_RENAMED_BUTTON = SEARCH.replace('"Search"', '"Find"')
"""The ``renamed_button`` inject: the submit control now reads "Find"."""

MEMBER_DETAIL = """
- text: MockCore Member Services 1.0
- heading "Member Detail" [level=3]
- table:
  - rowgroup:
    - row "Member ID 10003":
      - cell "Member ID"
      - cell "10003"
    - row "Name Test Member 03":
      - cell "Name"
      - cell "Test Member 03"
- heading "Balances" [level=3]
- table:
  - rowgroup:
    - row "Account Balance Status":
      - columnheader "Account"
      - columnheader "Balance"
      - columnheader "Status"
    - row "Savings $1,411.21 Open":
      - cell "Savings"
      - cell "$1,411.21"
      - cell "Open"
    - row "Checking $323.39 Open":
      - cell "Checking"
      - cell "$323.39"
      - cell "Open"
"""

SUBACCOUNT_WITH_VALIDATION = """
- text: MockCore Member Services 1.0
- heading "Open Sub-account" [level=3]
- table:
  - rowgroup:
    - row "Initial deposit is required":
      - cell "Initial deposit is required"
- table:
  - rowgroup:
    - row "Account Type Savings":
      - cell "Account Type"
      - cell "Savings":
        - combobox:
          - option "Savings" [selected]
          - option "Checking"
    - row "Initial Deposit":
      - cell "Initial Deposit"
      - cell:
        - textbox
    - row "Continue":
      - cell
      - cell "Continue":
        - button "Continue"
"""

SERVER_ERROR = """
- heading "Internal Server Error" [level=3]
- paragraph: The MockCore application encountered an unexpected condition (CICS ABEND ASRA).
"""

VIEWPORT = Viewport(w=1280, h=800)

# Layout constants. Chosen to resemble the real page: a label cell that ends
# where the next cell's control begins, and rows far enough apart that two
# stacked fields are not neighbours.
PAD = 10.0
ROW_H = 26.0
CELL_W = 120.0
LINE_H = 20.0
INSET = 3.0


def build(
    frames: dict[str, str],
    *,
    location: str = "http://127.0.0.1:8000/",
    urls: dict[str, str] | None = None,
    statuses: dict[str, int] | None = None,
    title: str = "MockCore 1.0",
    viewport: Viewport = VIEWPORT,
    frame_x: dict[str, float] | None = None,
) -> Observation:
    """Build an observation from one snapshot per frame."""
    urls = urls or {}
    statuses = statuses or {}
    frame_x = frame_x or {}

    nodes: list[Node] = []
    infos: list[FrameInfo] = []
    for name, snapshot in frames.items():
        infos.append(
            FrameInfo(
                name=name,
                url=urls.get(name, f"http://127.0.0.1:8000/{name or 'top'}"),
                status=statuses.get(name),
            )
        )
        parsed = flatten(parse_snapshot(snapshot), frame=name, start_index=len(nodes) + 1)
        nodes.extend(_lay_out(parsed, x0=frame_x.get(name, PAD)))

    return Observation(
        observed_at=datetime.now(UTC),
        location=location,
        title=title,
        viewport=viewport,
        frames=infos,
        nodes=annotate_near_text(nodes),
    )


def _lay_out(nodes: list[Node], *, x0: float) -> list[Node]:
    """Give every node the box a browser would give it, near enough."""
    by_ref = {n.ref: n for n in nodes}
    children: dict[str | None, list[Node]] = {}
    for node in nodes:
        children.setdefault(node.parent, []).append(node)

    boxes: dict[str, Rect] = {}
    cursor = PAD

    def row_of(node: Node) -> Node | None:
        current: Node | None = node
        while current is not None:
            if current.role == "row":
                return current
            current = by_ref.get(current.parent) if current.parent else None
        return None

    for node in nodes:
        if node.role == "row":
            top = cursor
            cursor += ROW_H
            x = x0
            for cell in _cells(node, children):
                boxes[cell.ref] = Rect(x=x, y=top, w=CELL_W, h=ROW_H)
                for inner in _descendants(cell, children):
                    boxes[inner.ref] = Rect(
                        x=x + INSET, y=top + INSET, w=CELL_W - 2 * INSET, h=ROW_H - 2 * INSET
                    )
                x += CELL_W
        elif node.role not in ("table", "rowgroup") and row_of(node) is None:
            boxes[node.ref] = Rect(x=x0, y=cursor, w=600.0, h=LINE_H)
            cursor += LINE_H + 4

    return [n.model_copy(update={"bbox": boxes.get(n.ref)}) for n in nodes]


def _cells(row: Node, children: dict[str | None, list[Node]]) -> list[Node]:
    return [n for n in _descendants(row, children) if n.role in ("cell", "columnheader")]


def _descendants(node: Node, children: dict[str | None, list[Node]]) -> list[Node]:
    out: list[Node] = []
    for child in children.get(node.ref, []):
        out.append(child)
        out.extend(_descendants(child, children))
    return out
