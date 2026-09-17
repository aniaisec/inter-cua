"""Conditions: what a step waits for, and what a detector matches on.

A capability's waits, checkpoints and outcome detectors are written in this
vocabulary rather than in Playwright calls, for two reasons.

**Portability.** ``location_matches`` is a URL on the web and a window title on
a desktop; ``region_present`` is a heading here and a pane title there. Each
condition below records its web interpretation and the desktop one it *would*
have. The desktop adapter is documented, not built — see ``DESKTOP_NOTES``.

**Purity.** Every condition is a function of an ``Observation``, never of a
live browser. A condition can therefore be evaluated against an observation
recorded a week ago, which is what makes replay's outcome detection
reproducible and testable without a browser, and what lets a failure report
state the condition it was waiting for next to the tree it actually saw.

The vocabulary is the one the capability schema uses:

``visible``                     a node is there and has a real box
``location_matches``            where the target is
``text_present``                words on the screen, optionally scoped to a frame
``region_present``              a named region of the screen has been reached
``error_banner_present``        the app itself failed
``validation_message_present``  the app rejected the input
``value_set``                   a control now holds a value
``output_extracted``            the run has captured a declared output

plus ``all_of`` / ``any_of``, which is all the logic a capability needs.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from cua.surface.a11y import normalize
from cua.surface.locators import Within
from cua.surface.protocol import CONTROL_ROLES, Node, Observation

SELF = "self"
"""``{target: self}`` means the node the step is acting on."""

VALIDATION_PROXIMITY_PX = 200.0
"""How close a message must sit to a control to read as being about that
control rather than about the screen."""

ERROR_STATUS_FROM = 500
"""Which HTTP statuses count as the app failing rather than the app answering.
A 4xx is usually the app saying no in a way the capability should classify as
a business outcome; a 5xx is the app falling over."""


class Visible(BaseModel):
    """Web: the node is in the tree with a non-empty box. Desktop: the control
    is on screen and not occluded by another window."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["visible"] = "visible"
    target: str = SELF


class LocationMatches(BaseModel):
    """Web: a regex against frame URLs. Desktop: against the window title.

    Every frame is searched, not just the top document. A frameset shell keeps
    its URL while the main pane navigates, so "are we on the member detail
    screen" is a question about the frames, and matching only the top document
    would answer "no" forever.
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal["location_matches"] = "location_matches"
    pattern: str
    within: Within | None = None


class TextPresent(BaseModel):
    """Web: the text appears in some node's name or value. Desktop: the same,
    over the platform's accessibility tree."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["text_present"] = "text_present"
    text: str
    within: Within | None = None


class RegionPresent(BaseModel):
    """Web: a heading or landmark with this name. Desktop: a pane or group.

    This is the condition a checkpoint should prefer over ``text_present``: a
    screen title is a claim about where you are, whereas stray text can match
    anywhere.
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal["region_present"] = "region_present"
    name: str
    within: Within | None = None


class ErrorBannerPresent(BaseModel):
    """The app failed, as opposed to answering.

    Web: a 5xx document response, or an ``alert`` node, or — for apps that
    render failures as ordinary prose, which this class of app does — matching
    ``text``. A modern app would be caught by the first two; a 1998 one needs
    the capability to supply the wording, which is why the wording lives in
    artifact data rather than in this module.
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal["error_banner_present"] = "error_banner_present"
    text: str | None = None
    within: Within | None = None


class ValidationMessagePresent(BaseModel):
    """The app rejected what was entered.

    Web: a message sitting next to a control — either an ``alert`` node, or
    text within ``VALIDATION_PROXIMITY_PX`` of an input, optionally narrowed by
    ``text`` and by ``near`` (the label of the field it belongs to). The
    proximity rule is what separates "Initial deposit is required" printed
    above a form from a paragraph of help text elsewhere on the screen.
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal["validation_message_present"] = "validation_message_present"
    text: str | None = None
    near: str | None = None
    within: Within | None = None


class ValueSet(BaseModel):
    """Web: the control holds a value, equal to ``value`` when one is given.

    A step that types into a field asserts this afterwards, so a field that
    silently refused the input (maxlength, a formatter, a disabled control)
    fails at the step that caused it instead of three screens later.
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal["value_set"] = "value_set"
    target: str = SELF
    value: str | None = None


class OutputExtracted(BaseModel):
    """A declared output has been captured. The only condition that asks about
    the run rather than the screen, which is why ``evaluate`` takes outputs."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["output_extracted"] = "output_extracted"
    name: str


class AllOf(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["all_of"] = "all_of"
    all_of: list[Condition]


class AnyOf(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["any_of"] = "any_of"
    any_of: list[Condition]


Condition: TypeAlias = Annotated[
    Visible
    | LocationMatches
    | TextPresent
    | RegionPresent
    | ErrorBannerPresent
    | ValidationMessagePresent
    | ValueSet
    | OutputExtracted
    | AllOf
    | AnyOf,
    Field(discriminator="kind"),
]

AllOf.model_rebuild()
AnyOf.model_rebuild()


DESKTOP_NOTES: dict[str, str] = {
    "visible": "control is on screen and not occluded by another window",
    "location_matches": "active window title, or the focused document's path",
    "text_present": "text anywhere in the platform accessibility tree",
    "region_present": "a pane, group or tab with this name",
    "error_banner_present": "a modal error dialog, or the app's status bar",
    "validation_message_present": "a field-level error balloon or status text",
    "value_set": "the control's Value pattern reads back what was entered",
    "output_extracted": "unchanged: a property of the run, not of the surface",
}
"""How a desktop adapter would read each condition. Recorded here so the claim
that the vocabulary is surface-neutral can be checked rather than believed;
no desktop adapter ships in this milestone.
"""


def evaluate(
    condition: Condition,
    observation: Observation,
    *,
    outputs: Mapping[str, object] | None = None,
    target: str | None = None,
) -> bool:
    """Is the condition true of this observation?

    ``target`` is the ref a ``{target: self}`` condition refers to — the node
    the current step is acting on.
    """
    if isinstance(condition, AllOf):
        return all(
            evaluate(c, observation, outputs=outputs, target=target) for c in condition.all_of
        )
    if isinstance(condition, AnyOf):
        return any(
            evaluate(c, observation, outputs=outputs, target=target) for c in condition.any_of
        )
    if isinstance(condition, Visible):
        node = _target_node(condition.target, observation, target)
        return node is not None and node.bbox is not None and not node.bbox.empty
    if isinstance(condition, LocationMatches):
        return _location_matches(condition, observation)
    if isinstance(condition, TextPresent):
        return _text_present(condition.text, _scope(observation, condition.within))
    if isinstance(condition, RegionPresent):
        wanted = normalize(condition.name)
        return any(
            n.role in ("heading", "region", "banner", "main", "form")
            and normalize(n.name) == wanted
            for n in _scope(observation, condition.within)
        )
    if isinstance(condition, ErrorBannerPresent):
        return _error_banner_present(condition, observation)
    if isinstance(condition, ValidationMessagePresent):
        return _validation_message_present(condition, observation) is not None
    if isinstance(condition, ValueSet):
        node = _target_node(condition.target, observation, target)
        if node is None or not node.value:
            return False
        return condition.value is None or normalize(node.value) == normalize(condition.value)
    return bool((outputs or {}).get(condition.name) is not None)


def validation_message(condition: ValidationMessagePresent, observation: Observation) -> str | None:
    """The message text itself, for a detector that reports it as a payload.

    Separate from ``evaluate`` so the condition vocabulary keeps its boolean
    contract while a detector can still say *what* the app complained about.
    """
    node = _validation_message_present(condition, observation)
    return node.text if node else None


def describe(condition: Condition) -> str:
    """One line naming what was expected, for a failure report."""
    if isinstance(condition, AllOf):
        return " and ".join(describe(c) for c in condition.all_of)
    if isinstance(condition, AnyOf):
        return " or ".join(describe(c) for c in condition.any_of)
    if isinstance(condition, LocationMatches):
        return f"location matches {condition.pattern!r}"
    if isinstance(condition, TextPresent):
        return f"text {condition.text!r} present"
    if isinstance(condition, RegionPresent):
        return f"region {condition.name!r} present"
    if isinstance(condition, ValueSet):
        return f"value set on {condition.target}" + (
            f" to {condition.value!r}" if condition.value else ""
        )
    if isinstance(condition, Visible):
        return f"{condition.target} visible"
    if isinstance(condition, OutputExtracted):
        return f"output {condition.name!r} extracted"
    if isinstance(condition, ErrorBannerPresent):
        return "error banner present"
    return "validation message present"


# --------------------------------------------------------------------------


def _scope(observation: Observation, within: Within | None) -> list[Node]:
    if within is None or within.frame is None:
        return list(observation.nodes)
    return observation.in_frame(within.frame)


def _target_node(target: str, observation: Observation, acting_on: str | None) -> Node | None:
    ref = acting_on if target == SELF else target
    return observation.find(ref) if ref else None


def _location_matches(condition: LocationMatches, observation: Observation) -> bool:
    pattern = re.compile(condition.pattern)
    frames = observation.frames
    if condition.within is not None and condition.within.frame is not None:
        frames = [f for f in frames if f.name == condition.within.frame]
    urls = [f.url for f in frames]
    if condition.within is None:
        urls.append(observation.location)
    return any(pattern.search(url) for url in urls)


def _text_present(text: str, nodes: list[Node]) -> bool:
    wanted = normalize(text)
    return any(wanted in normalize(n.text) for n in nodes)


def _error_banner_present(condition: ErrorBannerPresent, observation: Observation) -> bool:
    frames = observation.frames
    if condition.within is not None and condition.within.frame is not None:
        frames = [f for f in frames if f.name == condition.within.frame]
    if any(f.status is not None and f.status >= ERROR_STATUS_FROM for f in frames):
        return True
    nodes = _scope(observation, condition.within)
    if any(n.role == "alert" for n in nodes):
        return True
    return condition.text is not None and _text_present(condition.text, nodes)


def _validation_message_present(
    condition: ValidationMessagePresent, observation: Observation
) -> Node | None:
    nodes = _scope(observation, condition.within)
    controls = [n for n in nodes if n.role in CONTROL_ROLES and n.bbox is not None]
    if condition.near is not None:
        wanted = normalize(condition.near)
        controls = [c for c in controls if c.near_text and normalize(c.near_text) == wanted]

    for node in nodes:
        if node.role == "alert" and node.text:
            return node
        if not node.text or node.interactive or node.bbox is None:
            continue
        if condition.text is not None and normalize(condition.text) not in normalize(node.text):
            continue
        if condition.text is None and node.role not in ("text", "paragraph", "cell"):
            continue
        if any(_near_control(node, control) for control in controls if control.frame == node.frame):
            return node
    return None


def _near_control(message: Node, control: Node) -> bool:
    assert message.bbox is not None and control.bbox is not None
    m, c = message.bbox, control.bbox
    vertical = min(abs(c.y - m.bottom), abs(m.y - c.bottom), abs(c.y - m.y))
    horizontal = min(abs(c.x - m.right), abs(m.x - c.right), abs(c.x - m.x))
    return vertical <= VALIDATION_PROXIMITY_PX and horizontal <= VALIDATION_PROXIMITY_PX
