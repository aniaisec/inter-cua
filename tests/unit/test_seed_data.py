"""Guards on the seed data.

The brief forbids real credentials and PII. These assertions are cheap and they
fail loudly if someone later makes the fixtures "more realistic".
"""

from __future__ import annotations

import re

from mockapp.data import MEMBERS, format_currency, reference_number

NAME_PATTERN = re.compile(r"^Test Member \d{2}$")


def test_ten_members_with_the_expected_ids() -> None:
    assert sorted(MEMBERS) == [str(10000 + i) for i in range(1, 11)]


def test_every_name_is_obviously_synthetic() -> None:
    assert all(NAME_PATTERN.match(m.name) for m in MEMBERS.values())


def test_only_10007_is_restricted() -> None:
    restricted = {m.member_id for m in MEMBERS.values() if m.restricted}
    assert restricted == {"10007"}


def test_balances_are_distinct_so_a_wrong_extraction_cannot_pass() -> None:
    savings = [m.savings for m in MEMBERS.values()]
    assert len(set(savings)) == len(savings)


def test_currency_formatting_is_what_the_parser_will_see() -> None:
    assert format_currency(MEMBERS["10003"].savings).startswith("$")
    assert "," in format_currency(MEMBERS["10003"].savings)


def test_reference_numbers_are_deterministic() -> None:
    assert reference_number("10003", 1) == "REF-10003-0001"
