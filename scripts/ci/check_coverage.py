"""Require complete branch evidence for the small safety functions under test."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

TARGETS = {
    "src/cua/catalog.py": ("_as_text",),
    "src/cua/replay/invocation.py": ("check_input", "_bind_data"),
    "src/cua/replay/engine.py": ("ReplayEngine._expand.original",),
    "src/cua/surface/locators.py": ("_refusal",),
}


def check(report: dict[str, Any]) -> list[str]:
    if report.get("meta", {}).get("branch_coverage") is not True:
        raise ValueError("critical coverage requires branch collection")
    files = {name.replace("\\", "/"): data for name, data in report.get("files", {}).items()}
    checked = []
    for path, functions in TARGETS.items():
        for function in functions:
            evidence = files.get(path, {}).get("functions", {}).get(function)
            if not isinstance(evidence, dict):
                raise ValueError(f"missing function coverage: {path}:{function}")
            summary = evidence.get("summary", {})
            if (
                summary.get("num_statements", 0) <= 0
                or summary.get("num_branches", 0) <= 0
                or summary.get("missing_lines") != 0
                or summary.get("missing_branches") != 0
            ):
                raise ValueError(
                    f"critical function has uncovered statements/branches: {path}:{function}"
                )
            checked.append(f"{path}:{function}")
    return checked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        targets = check(json.loads(args.report.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        parser.exit(1, f"critical branch coverage gate failed: {exc}\n")
    print(f"Critical branch coverage: {len(targets)} functions fully exercised")


if __name__ == "__main__":
    main()
