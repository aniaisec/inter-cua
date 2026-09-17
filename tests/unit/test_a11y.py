"""Perception: snapshot text in, addressable tree out.

The fixtures in ``screens.py`` are captured from the real mock app, so these
tests fail if Playwright changes what it emits — which is the point. A silent
change in the snapshot format would otherwise surface much later as a locator
that mysteriously stopped resolving.
"""

from __future__ import annotations

import pytest

from cua.surface.a11y import SnapshotParseError, flatten, parse_snapshot
from tests.unit import screens


def roles(nodes, role):
    return [n for n in nodes if n.role == role]


def test_a_textbox_with_no_label_has_no_accessible_name() -> None:
    """The hostility contract the whole locator ladder is built around."""
    nodes = flatten(parse_snapshot(screens.LOGIN))
    textboxes = roles(nodes, "textbox")
    assert len(textboxes) == 2
    assert all(t.name == "" for t in textboxes)


def test_a_submit_button_is_named_by_its_value() -> None:
    nodes = flatten(parse_snapshot(screens.LOGIN))
    assert [b.name for b in roles(nodes, "button")] == ["Sign On"]


def test_a_filled_textbox_carries_its_value_not_a_name() -> None:
    nodes = flatten(parse_snapshot("- textbox: operator\n- paragraph: operator\n"))
    textbox, paragraph = nodes
    assert (textbox.value, textbox.name) == ("operator", "")
    assert (paragraph.value, paragraph.name) == (None, "operator")


def test_a_select_reports_the_selected_option_as_its_value() -> None:
    nodes = flatten(parse_snapshot(screens.SUBACCOUNT_WITH_VALIDATION))
    combobox = roles(nodes, "combobox")[0]
    assert combobox.value == "Savings"


def test_a_links_url_is_an_attribute_not_a_node() -> None:
    nodes = flatten(parse_snapshot(screens.NAV))
    link = roles(nodes, "link")[0]
    assert link.attrs["url"].endswith("/search'")
    assert not any(n.role.startswith("/") for n in nodes)


def test_attributes_are_kept() -> None:
    nodes = flatten(parse_snapshot(screens.LOGIN))
    assert roles(nodes, "heading")[0].attrs == {"level": "3"}


def test_screen_text_is_never_coerced_into_another_type() -> None:
    """A member id is a string of digits and a cell reading "No" is the word.

    PyYAML would give back an int and a bool, and both would go on to fail
    every name comparison in the locator rungs for reasons no one could see.
    """
    nodes = flatten(parse_snapshot('- cell: "10003"\n- cell: No\n- cell: 10003\n'))
    assert [n.name for n in nodes] == ["10003", "No", "10003"]


def test_refs_are_assigned_in_document_order_and_can_continue_across_frames() -> None:
    first = flatten(parse_snapshot(screens.NAV), frame="nav")
    second = flatten(parse_snapshot(screens.SEARCH), frame="main", start_index=len(first) + 1)
    assert first[0].ref == "n1"
    assert second[0].ref == f"n{len(first) + 1}"
    assert {n.frame for n in second} == {"main"}


def test_ordinals_count_by_role_so_an_unnamed_control_is_still_addressable() -> None:
    nodes = flatten(parse_snapshot(screens.LOGIN))
    assert [t.ordinal for t in roles(nodes, "textbox")] == [0, 1]
    assert roles(nodes, "button")[0].ordinal == 0


def test_the_tree_is_kept_so_a_cell_knows_its_row() -> None:
    nodes = flatten(parse_snapshot(screens.MEMBER_DETAIL))
    by_ref = {n.ref: n for n in nodes}
    savings = next(n for n in nodes if n.name == "$1,411.21")
    row = by_ref[savings.parent]
    assert row.role == "row"
    assert by_ref[by_ref[row.parent].parent].role == "table"


def test_an_unlabelled_control_is_described_by_the_text_beside_it() -> None:
    observation = screens.build({"main": screens.LOGIN})
    textboxes = roles(observation.nodes, "textbox")
    assert [t.near_text for t in textboxes] == ["User ID", "Password"]


def test_a_named_control_is_not_relabelled() -> None:
    observation = screens.build({"main": screens.SEARCH})
    button = roles(observation.nodes, "button")[0]
    assert button.near_text is None


def test_compaction_keeps_the_nav_links_and_the_search_box() -> None:
    """What the agent loop is handed for a signed-in screen."""
    observation = screens.build(
        {"nav": screens.NAV, "main": screens.SEARCH},
        frame_x={"nav": 10.0, "main": 200.0},
    )
    compact = observation.compact()

    assert "[nav]" in compact and "[main]" in compact
    assert 'link "Member Search"' in compact
    assert "textbox ~'Member ID'" in compact
    assert 'button "Search"' in compact
    # Containers carry no information a decision can be made from.
    assert "rowgroup" not in compact
    assert "\n  table" not in compact


def test_compaction_puts_a_row_on_one_line_with_its_controls_in_place() -> None:
    observation = screens.build({"main": screens.SEARCH})
    row = next(line for line in observation.compact().splitlines() if line.startswith("  row:"))
    assert row.count("|") == 2
    assert 'cell "Member ID"' in row and "textbox" in row and 'button "Search"' in row


def test_compaction_does_not_list_the_options_of_a_select() -> None:
    observation = screens.build({"main": screens.SUBACCOUNT_WITH_VALIDATION})
    compact = observation.compact()
    assert "combobox ='Savings'" in compact
    assert "option" not in compact


def test_an_empty_snapshot_is_an_empty_screen_not_an_error() -> None:
    assert parse_snapshot("") == []
    assert parse_snapshot("\n") == []


def test_a_snapshot_that_is_not_a_tree_is_rejected_loudly() -> None:
    with pytest.raises(SnapshotParseError):
        parse_snapshot("not: a list of nodes")
