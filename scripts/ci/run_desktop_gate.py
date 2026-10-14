"""Manual release gate: run real Windows tests, then verify the execution report."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.ci.check_test_results import check


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="New output directory")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.exit(1, "live desktop gate requires an unlocked interactive Windows session\n")
    try:
        import comtypes  # noqa: F401
    except ImportError:
        parser.exit(1, "install inter-cua[test,windows] for the live desktop gate\n")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    report = out / "desktop.xml"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/desktop",
            "--require-desktop",
            "--basetemp",
            str(out / "runtime"),
            "--junitxml",
            str(report),
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
    )
    if result.returncode:
        parser.exit(
            result.returncode, "live desktop tests failed; inspect desktop.xml and the log\n"
        )
    try:
        summary = check(report, allow_skip_prefix=("adapter does not claim ",))
    except (OSError, ET.ParseError, ValueError) as exc:
        parser.exit(1, f"live desktop execution report failed: {exc}\n")
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(
        f"Live desktop gate: {summary['executed']} executed, "
        f"{summary['skipped']} optional feature skips"
    )


if __name__ == "__main__":
    main()
