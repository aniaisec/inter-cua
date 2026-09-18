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
``dialog_raised``               the target raised a native dialog since the last look

plus ``all_of`` / ``any_of``, which is all the logic a capability needs.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from cua.surface.locators import Within

SELF = "self"
"""``{target: self}`` means the node the step is acting on."""

"""``{target: self}`` means the node the step is acting on."""


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
    text within ``SurfaceConfig.validation_proximity_px`` of an input, optionally narrowed by
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


class DialogRaised(BaseModel):
    """The target raised a native dialog since the previous observation.

    Web: an ``alert``/``confirm``/``prompt`` the surface intercepted. Desktop: a
    modal window owned by the application. Asked in the past tense on purpose:
    a native dialog blocks the whole session while it is open, so by the time
    anyone can ask, the surface has already answered it — as declared, or by
    dismissing it. ``expected=False`` narrows to dialogs the capability did not
    declare, which is the case replay escalates on.

    An in-page overlay is not a native dialog. It is ordinary markup, so it is
    found the way any screen text is found: ``text_present`` or
    ``region_present``.
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal["dialog_raised"] = "dialog_raised"
    text: str | None = None
    expected: bool | None = None


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
    | DialogRaised
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
    "dialog_raised": "a modal window owned by the app, seen via window events",
}
"""How a desktop adapter would read each condition. Recorded here so the claim
that the vocabulary is surface-neutral can be checked rather than believed;
no desktop adapter ships in this milestone.
"""


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
    if isinstance(condition, DialogRaised):
        qualifier = {None: "", True: "expected ", False: "unexpected "}[condition.expected]
        return f"{qualifier}dialog raised" + (
            f" saying {condition.text!r}" if condition.text else ""
        )
    return "validation message present"
