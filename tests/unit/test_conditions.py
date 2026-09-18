"""Conditions, evaluated against a recorded observation and no browser.

That they can be evaluated this way is the property the replay engine's
outcome detection depends on: the same condition that a step waited for can be
re-checked later against the evidence, and give the same answer.
"""

from __future__ import annotations

from cua.surface.conditions import (
    AllOf,
    AnyOf,
    ErrorBannerPresent,
    LocationMatches,
    OutputExtracted,
    RegionPresent,
    TextPresent,
    ValidationMessagePresent,
    ValueSet,
    Visible,
    describe,
)
from cua.surface.evaluators import WebEvaluator
from cua.surface.locators import Within

evaluate = WebEvaluator().evaluate
validation_message = WebEvaluator().validation_message
from tests.unit import screens

SHELL = "http://127.0.0.1:8000/"
SIGNED_IN_URLS = {"nav": "http://127.0.0.1:8000/nav", "main": "http://127.0.0.1:8000/search"}


def signed_in(main: str = screens.SEARCH, **kwargs):
    return screens.build(
        {"nav": screens.NAV, "main": main},
        location=SHELL,
        urls=SIGNED_IN_URLS,
        frame_x={"nav": 10.0, "main": 200.0},
        **kwargs,
    )


def detail(**kwargs):
    return screens.build(
        {"nav": screens.NAV, "main": screens.MEMBER_DETAIL},
        location=SHELL,
        urls={**SIGNED_IN_URLS, "main": "http://127.0.0.1:8000/member/10003"},
        **kwargs,
    )


# -- location --------------------------------------------------------------


def test_location_matches_looks_inside_the_frames_not_just_the_shell() -> None:
    """The frameset keeps its URL forever; the answer is in the main pane."""
    observation = detail()
    assert observation.location == SHELL
    assert evaluate(LocationMatches(pattern="/member/[0-9]+"), observation)


def test_location_can_be_pinned_to_one_frame() -> None:
    observation = detail()
    assert evaluate(LocationMatches(pattern="/member/", within=Within(frame="main")), observation)
    assert not evaluate(
        LocationMatches(pattern="/member/", within=Within(frame="nav")), observation
    )


def test_a_session_expiry_shows_up_as_the_main_pane_going_back_to_login() -> None:
    observation = signed_in(main=screens.LOGIN)
    expired = observation.model_copy(
        update={
            "frames": [
                f.model_copy(update={"url": "http://127.0.0.1:8000/login"})
                if f.name == "main"
                else f
                for f in observation.frames
            ]
        }
    )
    assert evaluate(LocationMatches(pattern="/login$"), expired)


# -- screen content --------------------------------------------------------


def test_text_present_reads_names_and_values() -> None:
    observation = detail()
    assert evaluate(TextPresent(text="Test Member 03"), observation)
    assert evaluate(TextPresent(text="$1,411.21"), observation)
    assert not evaluate(TextPresent(text="No matching member"), observation)


def test_text_present_can_be_scoped_to_a_frame() -> None:
    observation = signed_in()
    assert evaluate(TextPresent(text="Member Search", within=Within(frame="nav")), observation)
    assert not evaluate(
        TextPresent(text="Enter a five digit member number.", within=Within(frame="nav")),
        observation,
    )


def test_region_present_is_a_claim_about_where_you_are() -> None:
    observation = detail()
    assert evaluate(RegionPresent(name="Balances"), observation)
    assert evaluate(RegionPresent(name="Member Detail"), observation)
    assert not evaluate(RegionPresent(name="Review Sub-account"), observation)


def test_region_present_does_not_match_stray_text_that_happens_to_say_the_same() -> None:
    """A heading means "this screen"; the same words in a cell mean nothing."""
    observation = detail()
    assert evaluate(TextPresent(text="Savings"), observation)
    assert not evaluate(RegionPresent(name="Savings"), observation)


# -- controls --------------------------------------------------------------


def test_visible_needs_a_box_not_just_a_node() -> None:
    observation = signed_in()
    textbox = next(n for n in observation.nodes if n.role == "textbox")
    assert evaluate(Visible(), observation, target=textbox.ref)

    boxless = observation.model_copy(
        update={
            "nodes": [
                n.model_copy(update={"bbox": None}) if n.ref == textbox.ref else n
                for n in observation.nodes
            ]
        }
    )
    assert not evaluate(Visible(), boxless, target=textbox.ref)


def test_value_set_is_false_until_the_field_holds_something() -> None:
    observation = signed_in()
    empty = next(n for n in observation.nodes if n.role == "textbox")
    assert not evaluate(ValueSet(), observation, target=empty.ref)

    filled = observation.model_copy(
        update={
            "nodes": [
                n.model_copy(update={"value": "10003"}) if n.ref == empty.ref else n
                for n in observation.nodes
            ]
        }
    )
    assert evaluate(ValueSet(), filled, target=empty.ref)
    assert evaluate(ValueSet(value="10003"), filled, target=empty.ref)
    assert not evaluate(ValueSet(value="10007"), filled, target=empty.ref)


def test_a_condition_about_self_with_no_self_is_false_not_an_error() -> None:
    assert not evaluate(ValueSet(), signed_in())


# -- the app failing, and the app saying no --------------------------------


def test_error_banner_present_catches_a_500_with_no_aria_to_go_on() -> None:
    observation = screens.build(
        {"main": screens.SERVER_ERROR},
        urls={"main": "http://127.0.0.1:8000/member/10003"},
        statuses={"main": 500},
    )
    assert evaluate(ErrorBannerPresent(), observation)


def test_a_working_screen_is_not_an_error_banner() -> None:
    assert not evaluate(ErrorBannerPresent(), detail(statuses={"main": 200}))


def test_error_banner_can_be_recognised_by_the_apps_own_wording() -> None:
    """Legacy failures are prose. The wording belongs to the capability."""
    observation = screens.build({"main": screens.SERVER_ERROR})
    assert evaluate(ErrorBannerPresent(text="CICS ABEND"), observation)
    assert not evaluate(ErrorBannerPresent(text="CICS ABEND"), detail())


def test_a_validation_message_is_one_that_sits_next_to_the_form() -> None:
    observation = screens.build({"main": screens.SUBACCOUNT_WITH_VALIDATION})
    condition = ValidationMessagePresent(text="Initial deposit is required")
    assert evaluate(condition, observation)
    assert validation_message(condition, observation) == "Initial deposit is required"


def test_a_screen_with_no_complaint_has_no_validation_message() -> None:
    observation = screens.build({"main": screens.SEARCH})
    assert not evaluate(ValidationMessagePresent(text="Initial deposit is required"), observation)


# -- run state, and combinators -------------------------------------------


def test_output_extracted_asks_about_the_run_not_the_screen() -> None:
    observation = detail()
    assert not evaluate(OutputExtracted(name="savings_balance"), observation)
    assert evaluate(
        OutputExtracted(name="savings_balance"),
        observation,
        outputs={"savings_balance": "1411.21"},
    )
    assert not evaluate(
        OutputExtracted(name="savings_balance"), observation, outputs={"savings_balance": None}
    )


def test_a_checkpoint_is_a_compound_claim() -> None:
    """``cp.member_detail`` from the artifact: the URL, the id, and the region."""
    checkpoint = AllOf(
        all_of=[
            LocationMatches(pattern="/member/[0-9]+"),
            TextPresent(text="10003"),
            RegionPresent(name="Balances"),
        ]
    )
    assert evaluate(checkpoint, detail())
    assert not evaluate(checkpoint, signed_in())


def test_a_step_can_expect_either_of_two_outcomes() -> None:
    """``search.submit`` lands on a member, or says there is no such member."""
    either = AnyOf(
        any_of=[
            LocationMatches(pattern="/member/"),
            TextPresent(text="No matching member"),
        ]
    )
    assert evaluate(either, detail())
    assert not evaluate(either, signed_in())


def test_conditions_can_say_what_they_were_waiting_for() -> None:
    """A failure report is only debuggable if it names the expectation."""
    condition = AllOf(all_of=[LocationMatches(pattern="/member/"), RegionPresent(name="Balances")])
    assert describe(condition) == "location matches '/member/' and region 'Balances' present"
