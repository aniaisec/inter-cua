"""Whether a visual match may become an action: every check, one function.

Pure, like the ladder and the policy: the same matches, step, screen and rules
give the same answer, so the whole gate is tested without a browser. The
checks run cheapest first, and the first to fail is the answer:

``unsupported``   vision proposes clicks, nothing else
``unsafe``        the step commits something, or needs approval: a person does it
``scale``         the screenshot is not at the scale the picture was recorded at
``not_found``     no match is good enough (``VisionRules.min_confidence``)
``ambiguous``     another match is nearly as good (``VisionRules.min_margin``);
                  vision never chooses between two controls that look alike
``outside``       the match is not wholly on the screen
``masked``        the match touches a masked region, whose pixels are not the
                  screen's and must never be acted on
``contradicted``  the tree has a control at that point: the tree names what is
                  there, and a picture does not overrule it
``policy``        the policy blocks the click, or a risky rule covers it

Only a match that passes all of them becomes a ``VisualCandidate``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from cua.policy.allowlist import Allow, Block, Policy, check
from cua.surface.locators import BBox, Ladder, NearText, RoleName
from cua.surface.protocol import (
    INTERACTIVE_ROLES,
    TOP_FRAME,
    ClickPoint,
    Node,
    Observation,
    Rect,
)
from cua.surface.vision import image
from cua.surface.vision.candidate import Appearance, VisualCandidate
from cua.surface.vision.detector import VisualMatch

RefusalKind = Literal[
    "unsupported",
    "unsafe",
    "scale",
    "not_found",
    "ambiguous",
    "outside",
    "masked",
    "contradicted",
    "policy",
]
SCALE_TOLERANCE = 0.01


class StepClaim(BaseModel):
    """What the step says it does, as far as vision is concerned."""

    model_config = ConfigDict(frozen=True)

    step_id: str
    action: str
    risk: Literal["safe", "risky", "irreversible"]
    approval: Literal["none", "required"]
    target: Ladder
    appearance: Appearance


class Accepted(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["accepted"] = "accepted"
    candidate: VisualCandidate


class Refused(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["refused"] = "refused"
    why: RefusalKind
    reason: str
    best: float | None = None
    """The best match's confidence, when there was one."""


Verdict = Accepted | Refused


def validate(
    matches: list[VisualMatch],
    *,
    claim: StepClaim,
    screen: Observation,
    screenshot_png: bytes,
    masks: list[Rect],
    policy: Policy,
    evidence_ref: str,
) -> Verdict:
    """``matches`` are the detector's, in screenshot pixels, best first.
    ``masks`` are the boxes painted out of that screenshot."""
    rules = policy.vision
    best = matches[0].confidence if matches else None

    def refuse(why: RefusalKind, reason: str) -> Refused:
        return Refused(why=why, reason=reason, best=best)

    if claim.action != "click":
        return refuse(
            "unsupported", f"vision proposes clicks, and {claim.step_id} is a {claim.action}"
        )
    if claim.risk != "safe" or claim.approval != "none":
        return refuse(
            "unsafe",
            f"{claim.step_id} is {claim.risk}"
            + (" and needs approval" if claim.approval == "required" else "")
            + "; a step that commits something is never done by pixels",
        )
    scale = image.scale_of(screenshot_png, screen.viewport)
    if abs(scale - claim.appearance.scale) > SCALE_TOLERANCE:
        return refuse(
            "scale",
            f"the screen is at scale {scale:g} and the control was recorded at "
            f"{claim.appearance.scale:g}; a picture is not matched across sizes",
        )
    good = [m for m in matches if m.confidence >= rules.min_confidence]
    if not good:
        seen = f"; the best match scored {best:.3f}" if best is not None else ""
        return refuse(
            "not_found",
            f"nothing on the screen looks like the recorded control "
            f"(needs {rules.min_confidence:.2f}{seen})",
        )
    top = good[0]
    rivals = [m for m in matches[1:] if m.confidence >= top.confidence - rules.min_margin]
    if rivals:
        return refuse(
            "ambiguous",
            f"{len(rivals) + 1} places look like the recorded control "
            f"({', '.join(f'{m.confidence:.3f}' for m in [top, *rivals])}); "
            "vision does not choose between them",
        )

    box = _to_viewport(top.bounds, scale)
    if box.x < 0 or box.y < 0 or box.right > screen.viewport.w or box.bottom > screen.viewport.h:
        return refuse("outside", "the match is not wholly on the screen")
    if any(_overlaps(box, m) for m in masks):
        return refuse("masked", "the match touches a masked region, which is never acted on")
    cx, cy = box.center
    here = _control_at(screen, cx, cy)
    if here is not None:
        return refuse(
            "contradicted",
            f"the accessibility tree has {here.label} at that point; "
            "a picture does not overrule the tree",
        )

    role, name, frame = _claimed(claim.target)
    action = ClickPoint(
        x=cx,
        y=cy,
        guard=box,
        guard_sha256=image.pixels_sha256(screenshot_png, _to_pixels(box, scale)),
        role=role,
        name=name,
        frame=frame,
    )
    decision = check(policy, action, screen)
    if isinstance(decision, Block):
        return refuse("policy", decision.reason)
    if not isinstance(decision, Allow) or decision.approved_rule is not None:
        return refuse(
            "policy",
            f"{decision.reason if not isinstance(decision, Allow) else 'a risky rule'} "
            "applies here; a click by pixels is never made where a commit rule does",
        )
    return Accepted(
        candidate=VisualCandidate(
            action=action,
            confidence=top.confidence,
            bounds=box,
            evidence_ref=evidence_ref,
        )
    )


def _claimed(ladder: Ladder) -> tuple[str | None, str, str]:
    """Role, name and frame of the control the ladder names, from its most
    specific rung."""
    role: str | None = None
    name = ""
    frame = TOP_FRAME
    for rung in ladder:
        if rung.within is not None and rung.within.frame is not None and frame == TOP_FRAME:
            frame = rung.within.frame
        if isinstance(rung, RoleName):
            return rung.role, rung.name, frame
        if isinstance(rung, NearText | BBox) and role is None:
            role = rung.role
    return role, name, frame


def _control_at(screen: Observation, x: float, y: float) -> Node | None:
    for node in screen.nodes:
        box = node.bbox
        if node.role not in INTERACTIVE_ROLES or box is None or box.empty:
            continue
        if box.x <= x <= box.right and box.y <= y <= box.bottom:
            return node
    return None


def _overlaps(a: Rect, b: Rect) -> bool:
    return a.x < b.right and b.x < a.right and a.y < b.bottom and b.y < a.bottom


def _to_viewport(box: Rect, scale: float) -> Rect:
    if scale == 1.0:
        return box
    return Rect(x=box.x / scale, y=box.y / scale, w=box.w / scale, h=box.h / scale)


def _to_pixels(box: Rect, scale: float) -> Rect:
    if scale == 1.0:
        return box
    return Rect(x=box.x * scale, y=box.y * scale, w=box.w * scale, h=box.h * scale)
