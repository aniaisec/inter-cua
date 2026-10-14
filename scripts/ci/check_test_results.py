"""Summarize executed/skipped JUnit cases and refuse empty or incomplete gates."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def check(path: Path, *, allow_skip_prefix: tuple[str, ...] = ()) -> dict[str, object]:
    root = ET.parse(path).getroot()
    if root.tag not in ("testsuite", "testsuites"):
        raise ValueError("expected a JUnit testsuite or testsuites report")
    cases = list(root.iter("testcase"))
    if not cases or not list(root.iter("testsuite")):
        raise ValueError("gate has no test cases")
    failures = list(root.iter("failure")) + list(root.iter("error"))
    skipped = [case for case in cases if case.find("skipped") is not None]
    passed = (
        len(cases)
        - len(skipped)
        - sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
    )
    # Suite-level errors (including collection errors) need not be in a testcase.
    if failures or any(
        int(s.get("errors", "0")) or int(s.get("failures", "0")) for s in root.iter("testsuite")
    ):
        raise ValueError("gate includes failed tests or collection errors")
    if passed <= 0:
        raise ValueError("gate executed no passing tests")
    for case in skipped:
        skipped_node = case.find("skipped")
        assert skipped_node is not None
        reason = skipped_node.get("message", "")
        if not allow_skip_prefix or not reason.startswith(allow_skip_prefix):
            raise ValueError(f"unexpected skip: {case.get('name')}: {reason}")
    return {
        "tests": len(cases),
        "executed": len(cases) - len(skipped),
        "passed": passed,
        "skipped": len(skipped),
        "skipped_cases": [case.get("name") for case in skipped],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--allow-skip-prefix", action="append", default=[])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        summary = check(args.report, allow_skip_prefix=tuple(args.allow_skip_prefix))
    except (OSError, ET.ParseError, ValueError) as exc:
        parser.exit(1, f"test result gate failed: {exc}\n")
    text = json.dumps(summary, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
