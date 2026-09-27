"""From a node back to the control it was read from, checked before acting.

Finding *which* node a capability means is the locator ladder's job, and it is
the same ladder the web uses (``cua.surface.locators``): ``role_name`` is role
plus name, ``near_text`` is the label beside a control, ``table_cell`` is the
container, ``bbox`` is raw position, tried last and never acted on unattended.
Nothing here re-ranks it.

What is desktop-specific is the last step: the node came from one
observation, and the element behind it is live. Just before acting, the
element is read again, and it must still be the control that was observed —
same role, same name, near the same place. A window that re-laid itself out
between looking and acting raises ``PerceptionDrift`` instead of a click on
whatever moved into that place, as ``PlaywrightSurface._check_identity`` does.
"""

from __future__ import annotations

from collections.abc import Callable

from cua.surface.protocol import Node, PerceptionDrift, Rect
from cua.surface.windows.perception import ROLES, RawElement

DRIFT_TOLERANCE_PX = 8.0
"""As the web adapter's: a control that changed rows moves by a row height,
well past this; a re-layout by a pixel does not reach it."""


def confirm(
    node: Node,
    observed: RawElement,
    read_now: Callable[[RawElement], RawElement],
    origin: tuple[float, float],
) -> RawElement:
    """The element behind ``node``, re-read and checked; ``origin`` is the
    window's corner now, since boxes are window-relative."""
    try:
        now = read_now(observed)
    except Exception as exc:  # a COM error: the element has gone
        raise PerceptionDrift(f"{node.label} is no longer on screen ({exc})") from None
    role = ROLES.get(now.control_type, "generic")
    if role != node.role:
        raise PerceptionDrift(f"{node.label} is now a {role}")
    if (now.name or "") != node.name:
        raise PerceptionDrift(f"{node.label} is now named {now.name!r}")
    if node.bbox is not None and _moved(node.bbox, now, origin):
        raise PerceptionDrift(f"{node.label} moved since it was observed")
    return now


def _moved(seen: Rect, now: RawElement, origin: tuple[float, float]) -> bool:
    left, top, _, _ = now.rect
    return (
        abs((left - origin[0]) - seen.x) > DRIFT_TOLERANCE_PX
        or abs((top - origin[1]) - seen.y) > DRIFT_TOLERANCE_PX
    )
