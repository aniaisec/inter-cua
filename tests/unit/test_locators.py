"""The locator ladder: resolve exactly one node, or fall through saying why."""

from __future__ import annotations

from cua.surface.locators import (
    Ambiguous,
    BBox,
    NearText,
    Resolved,
    RoleName,
    TableCell,
    Unresolved,
    Within,
    match,
    resolve_ladder,
)
from cua.surface.protocol import RecordingEnv, Viewport
from tests.unit import screens

SIGNED_IN = {"nav": screens.NAV, "main": screens.SEARCH}
FRAME_X = {"nav": 10.0, "main": 200.0}

# The ladder a recorded capability carries for the search screen's submit
# button: the name first, the label beside the field as the fallback.
SEARCH_SUBMIT = [
    RoleName(role="button", name="Search"),
    NearText(text="Member ID", role="button"),
]

MEMBER_ID_FIELD = [NearText(text="Member ID", role="textbox")]


def signed_in(nav: str = screens.NAV, main: str = screens.SEARCH):
    return screens.build({"nav": nav, "main": main}, frame_x=FRAME_X)


# -- each rung resolves ----------------------------------------------------


def test_role_name_resolves_a_named_control() -> None:
    outcome = resolve_ladder(SEARCH_SUBMIT, signed_in())
    assert isinstance(outcome, Resolved)
    assert (outcome.node.role, outcome.node.name) == ("button", "Search")
    assert outcome.rung == "role_name"
    assert not outcome.slipped


def test_near_text_resolves_a_control_with_no_name_at_all() -> None:
    outcome = resolve_ladder(MEMBER_ID_FIELD, signed_in())
    assert isinstance(outcome, Resolved)
    assert outcome.node.role == "textbox"
    assert outcome.node.name == ""
    assert outcome.node.frame == "main"


def test_near_text_does_not_reach_across_a_frame_boundary() -> None:
    """A label in the nav pane does not label a field in the main pane.

    Boxes are comparable across frames, so nothing but this rule stops the
    rung from pairing a control with whatever text happens to sit beside it in
    the other pane.
    """
    observation = signed_in(nav=screens.NAV_WITH_SECOND_SEARCH)
    found = match(NearText(text="Member ID", role="textbox"), observation)
    assert [n.frame for n in found] == ["main"]


def test_table_cell_reads_a_grid_the_way_a_human_does() -> None:
    observation = screens.build({"main": screens.MEMBER_DETAIL})
    outcome = resolve_ladder(
        [TableCell(row_contains="Savings", column_header="Balance")], observation
    )
    assert isinstance(outcome, Resolved)
    assert outcome.node.name == "$1,411.21"


def test_table_cell_is_not_confused_by_a_second_table_on_the_screen() -> None:
    observation = screens.build({"main": screens.MEMBER_DETAIL})
    found = match(TableCell(row_contains="Checking", column_header="Status"), observation)
    assert [n.name for n in found] == ["Open"]


def test_bbox_resolves_in_the_viewport_it_was_recorded_in() -> None:
    observation = screens.build({"main": screens.SEARCH})
    textbox = next(n for n in observation.nodes if n.role == "textbox")
    box = textbox.bbox
    assert box is not None
    outcome = resolve_ladder(
        [BBox(x=box.x, y=box.y, w=box.w, h=box.h, role="textbox")],
        observation,
        recording_env=RecordingEnv(viewport=screens.VIEWPORT),
    )
    assert isinstance(outcome, Resolved)
    assert outcome.node.ref == textbox.ref


def test_bbox_collapses_a_control_and_the_cell_wrapping_it() -> None:
    """Containment is not ambiguity: the inner node is the answer."""
    observation = screens.build({"main": screens.SEARCH})
    textbox = next(n for n in observation.nodes if n.role == "textbox")
    assert textbox.bbox is not None
    found = match(
        BBox(x=textbox.bbox.x, y=textbox.bbox.y, w=textbox.bbox.w, h=textbox.bbox.h), observation
    )
    assert [n.ref for n in found] == [textbox.ref]


# -- falling through -------------------------------------------------------


def test_a_renamed_button_leaves_the_ladder_unresolved_not_wrong() -> None:
    """``renamed_button``: "Search" became "Find" and nothing else can find it."""
    observation = signed_in(main=screens.SEARCH_RENAMED_BUTTON)
    outcome = resolve_ladder([RoleName(role="button", name="Search")], observation)
    assert isinstance(outcome, Unresolved)
    assert outcome.attempts[0].matches == 0
    assert outcome.attempts[0].rung == "role_name"


def test_a_renamed_button_is_still_found_by_the_label_beside_it() -> None:
    """The fall-through that keeps a rename from being an outage."""
    observation = signed_in(main=screens.SEARCH_RENAMED_BUTTON)
    outcome = resolve_ladder(SEARCH_SUBMIT, observation)
    assert isinstance(outcome, Resolved)
    assert outcome.node.name == "Find"
    assert outcome.rung == "near_text"
    assert outcome.slipped


def test_two_buttons_with_the_same_name_make_the_rung_fall_through() -> None:
    """``ambiguous_button``: the nav frame grows a second "Search".

    The strong rung now matches two nodes in different frames. Picking either
    would be a coin flip, so it falls through to the rung that can tell them
    apart — and the run records that it had to.
    """
    observation = signed_in(nav=screens.NAV_WITH_SECOND_SEARCH)
    outcome = resolve_ladder(SEARCH_SUBMIT, observation)

    assert isinstance(outcome, Resolved)
    assert outcome.rung == "near_text"
    assert outcome.slipped
    assert outcome.node.frame == "main"
    assert outcome.attempts[0].rung == "role_name"
    assert outcome.attempts[0].matches == 2


def test_a_frame_scoped_rung_is_not_ambiguous_in_the_first_place() -> None:
    observation = signed_in(nav=screens.NAV_WITH_SECOND_SEARCH)
    outcome = resolve_ladder(
        [RoleName(role="button", name="Search", within=Within(frame="main"))], observation
    )
    assert isinstance(outcome, Resolved)
    assert outcome.node.frame == "main"


def test_a_ladder_that_only_ever_matched_too_much_is_ambiguous_not_missing() -> None:
    """The two failures need different fixes, so they are different answers."""
    observation = signed_in(nav=screens.NAV_WITH_SECOND_SEARCH)
    outcome = resolve_ladder([RoleName(role="button", name="Search")], observation)
    assert isinstance(outcome, Ambiguous)
    assert outcome.matches == 2


def test_a_ladder_with_nothing_left_to_try_is_unresolved() -> None:
    observation = signed_in()
    outcome = resolve_ladder(
        [
            RoleName(role="button", name="Post Transaction"),
            NearText(text="Account Number", role="textbox"),
        ],
        observation,
    )
    assert isinstance(outcome, Unresolved)
    assert [a.matches for a in outcome.attempts] == [0, 0]


# -- pixels are not portable ----------------------------------------------


def test_bbox_is_refused_at_a_different_viewport() -> None:
    observation = screens.build({"main": screens.SEARCH}, viewport=Viewport(w=800, h=600))
    textbox = next(n for n in observation.nodes if n.role == "textbox")
    assert textbox.bbox is not None
    outcome = resolve_ladder(
        [BBox(x=textbox.bbox.x, y=textbox.bbox.y, w=textbox.bbox.w, h=textbox.bbox.h)],
        observation,
        recording_env=RecordingEnv(viewport=Viewport(w=1280, h=800)),
    )
    assert isinstance(outcome, Unresolved)
    assert outcome.attempts[0].refused is not None
    assert "800x600" in outcome.attempts[0].refused


def test_bbox_is_refused_when_nothing_says_where_it_was_recorded() -> None:
    observation = screens.build({"main": screens.SEARCH})
    outcome = resolve_ladder([BBox(x=0, y=0, w=1280, h=800)], observation)
    assert isinstance(outcome, Unresolved)
    assert outcome.attempts[0].refused is not None


def test_a_refused_rung_does_not_stop_the_ladder() -> None:
    observation = screens.build({"main": screens.SEARCH}, viewport=Viewport(w=800, h=600))
    outcome = resolve_ladder(
        [BBox(x=0, y=0, w=10, h=10), RoleName(role="button", name="Search")],
        observation,
        recording_env=RecordingEnv(viewport=Viewport(w=1280, h=800)),
    )
    assert isinstance(outcome, Resolved)
    assert outcome.rung == "role_name"
    assert outcome.attempts[0].refused is not None
