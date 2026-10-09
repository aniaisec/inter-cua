"""Execute one documented scenario: python -m examples.run lookup --out PATH.

Every scenario has its own initialized project and target process. Commands and
results are recorded without approval/resume tokens; runtime gates are the real CLI's.
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from examples._session import EXAMPLES, Session
from examples._support import ExampleError, check_subset, require

NAMES = (
    "lookup",
    "business_rejection",
    "approved_submission",
    "drift_repair",
    "human_takeover",
    "workflow",
    "http_client",
    "mcp_setup",
    "windows_uia",
)


def scenario(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((EXAMPLES / name / "scenario.json").read_text())
    require(data["schema_version"] == 1 and data["name"] == name, "invalid scenario identity")
    return data


def run_scenario(name: str, out: Path) -> dict[str, Any]:
    spec = scenario(name)
    if name == "windows_uia":
        require(sys.platform == "win32", "Windows UIA requires an interactive Windows session")
    s = Session(spec, out)
    try:
        with ExitStack() as stack:
            # Cleanup runs before application/console servers are stopped.
            try:
                s.bootstrap(stack)
                result = importlib.import_module(f"examples.{name}.run").run(s, stack)
                check_subset(result, spec["expected"])
            finally:
                s.cleanup_browsers()
        summary = {"schema_version": 1, "scenario": name, "passed": True, "result": result}
    except Exception as exc:
        (s.out / "summary.json").write_text(
            json.dumps({"scenario": name, "passed": False, "error": str(exc)}, indent=2)
        )
        raise
    (s.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=(*NAMES, "all"))
    parser.add_argument(
        "--out", type=Path, required=True, help="New output directory; never overwritten"
    )
    parser.add_argument(
        "--include-desktop", action="store_true", help="Opt into the interactive Windows gate"
    )
    args = parser.parse_args()
    names = (
        [name for name in NAMES if name != "windows_uia" or args.include_desktop]
        if args.scenario == "all"
        else [args.scenario]
    )
    if args.scenario == "all":
        args.out.mkdir(parents=True, exist_ok=False)
    try:
        for name in names:
            out = args.out / name if args.scenario == "all" else args.out
            result = run_scenario(name, out)
            print(json.dumps(result), flush=True)
    except (ExampleError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"example failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
