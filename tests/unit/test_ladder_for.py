"""Turning a node the agent used into a ladder that finds it again."""

from __future__ import annotations

from cua.agent.stopping import fingerprint
from cua.surface.locators import Resolved, ladder_for, resolve_ladder
from tests.unit import screens

FRAME_X = {"nav": 10.0, "main": 200.0}


def detail():
    return screens.build({"nav": screens.NAV, "main": screens.MEMBER_DETAIL}, frame_x=FRAME_X)


def node(obs, role: str, name: str = ""):
    return next(n for n in obs.nodes if n.role == role and n.name == name)


def test_a_named_button_is_found_by_role_and_name_in_its_own_frame() -> None:
    obs = screens.build({"nav": screens.NAV, "main": screens.SEARCH}, frame_x=FRAME_X)
    ladder = ladder_for(node(obs, "button", "Search"), obs)
    assert ladder[0].strategy == "role_name"
    assert ladder[0].within is not None and ladder[0].within.frame == "main"


def test_an_unnamed_field_is_found_by_the_label_beside_it() -> None:
    obs = screens.build({"": screens.LOGIN})
    field = next(n for n in obs.nodes if n.role == "textbox" and n.near_text == "Password")
    ladder = ladder_for(field, obs)
    assert [r.strategy for r in ladder] == ["near_text", "bbox"]


def test_a_value_is_named_by_where_it_sits_never_by_what_it_says() -> None:
    obs = detail()
    balance = node(obs, "cell", "$1,411.21")
    ladder = ladder_for(balance, obs, for_value=True)

    assert ladder[0].strategy == "table_cell"
    assert (ladder[0].row_contains, ladder[0].column_header) == ("Savings", "Balance")
    assert all("1,411" not in r.model_dump_json() for r in ladder if r.strategy != "bbox")


def test_a_label_value_pair_without_headers_is_named_by_its_label() -> None:
    obs = detail()
    name = node(obs, "cell", "Test Member 03")
    ladder = ladder_for(name, obs, for_value=True)
    # The member table has no header row: its first row is data, not a header.
    assert all(r.strategy != "table_cell" for r in ladder)
    assert ladder[0].strategy == "near_text"
    assert ladder[0].text == "Name"


def test_every_rung_kept_resolves_back_to_the_node_and_only_it() -> None:
    obs = detail()
    for target in obs.nodes:
        for rung in ladder_for(target, obs, for_value=target.role == "cell"):
            outcome = resolve_ladder([rung], obs) if rung.strategy != "bbox" else None
            if outcome is not None:
                assert isinstance(outcome, Resolved) and outcome.ref == target.ref


def test_the_same_screen_has_the_same_fingerprint_whatever_its_refs() -> None:
    first = screens.build({"": screens.SEARCH})

    def shift(ref: str | None) -> str | None:
        return f"n{int(ref[1:]) + 500}" if ref else None

    renumbered = first.model_copy(
        update={
            "nodes": [
                n.model_copy(update={"ref": shift(n.ref), "parent": shift(n.parent)})
                for n in first.nodes
            ]
        }
    )
    assert first.nodes[0].ref != renumbered.nodes[0].ref
    assert fingerprint(first) == fingerprint(renumbered)
    assert fingerprint(first) != fingerprint(screens.build({"": screens.LOGIN}))
