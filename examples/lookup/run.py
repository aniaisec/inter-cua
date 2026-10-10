"""Execute CLI cases from scenario.json."""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any

from examples._session import Session, cli_cases


def run(s: Session, stack: ExitStack) -> dict[str, Any]:
    return cli_cases(s)
