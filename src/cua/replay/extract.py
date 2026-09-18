"""Turning what a screen says into a typed output.

Parsing is strict. ``"$1,411.21"`` is a ``currency_usd`` and becomes
``"1411.21"``; ``"(12.50)"`` is the accounting way of writing a negative;
``"n/a"`` is not a decimal and is an ``EXTRACTION_FAILED`` rather than a
``None`` that a caller would read as zero.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from cua.artifact.schema import OutputSpec
from cua.replay.result import OutputValue


class ParseError(ValueError):
    pass


def parse(text: str, spec: OutputSpec) -> OutputValue:
    value = text.strip()
    if not value:
        raise ParseError("it is empty")
    how = spec.extract.parse
    if how == "text" and spec.type == "string":
        return value
    number = _number(value, currency=how == "currency_usd")
    if spec.type == "integer" or how == "integer":
        if number != number.to_integral_value():
            raise ParseError(f"{value!r} is not a whole number")
        return int(number)
    if spec.type == "string":
        return value
    return str(number)


def _number(value: str, *, currency: bool) -> Decimal:
    text = value.replace(",", "").strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").strip()
    if text.startswith("-"):
        negative, text = not negative, text[1:].strip()
    if currency:
        text = text.removeprefix("$").strip()
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", text):
        raise ParseError(f"{value!r} is not a {'dollar amount' if currency else 'number'}")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - the regex already refused it
        raise ParseError(f"{value!r} is not a number") from exc
    return -number if negative else number
