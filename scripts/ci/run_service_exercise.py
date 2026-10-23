"""Run isolated synthetic PR17 regressions and record candidate-bound evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.ci.check_service_release import BLOCKERS, EXERCISES, MANUAL_GATES, check_exercise


def run(out: Path) -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dirty = bool(
        subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip()
    )
    # Never overwrite candidate evidence, including a partially failed attempt.
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    exercises = {}
    failures = {}
    for name, selectors in EXERCISES.items():
        report = out / f"{name}.xml"
        with (
            tempfile.TemporaryDirectory(prefix="runtime-", dir=out) as runtime,
            (out / f"{name}.log").open("w", encoding="utf-8") as log,
        ):
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-v",
                        *selectors,
                        f"--junitxml={report}",
                        f"--basetemp={runtime}/cases",
                    ],
                    cwd=root,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=120,
                )
            except subprocess.TimeoutExpired:
                failures[name] = "pytest exceeded the 120 second exercise deadline"
                continue
        try:
            if result.returncode:
                raise ValueError(f"pytest exited {result.returncode}")
            check_exercise(name, report)
        except (OSError, ValueError, ET.ParseError) as exc:
            failures[name] = str(exc)
        if report.is_file():
            exercises[name] = {
                "path": report.name,
                "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            }
    # Detect tracked/untracked source edits made during the exercise as well.
    dirty = dirty or bool(
        subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip()
    )
    current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    record = {
        "schema_version": 1,
        "commit": commit,
        "source_dirty": dirty or current != commit,
        "environment": f"{platform.platform()} / Python {platform.python_version()}",
        "exercises": exercises,
        "manual_gates": {name: {"status": "pending", "evidence": None} for name in MANUAL_GATES},
        "open_blockers": BLOCKERS,
    }
    (out / "record.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    summary: dict[str, object] = {
        "status": "failed" if failures else "automated_checks_passed",
        "release_status": "not_qualified",
        "failures": failures,
        "record": str(out / "record.json"),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="New private evidence directory")
    args = parser.parse_args()
    try:
        summary = run(args.out)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"service exercise failed: {exc}\n")
    print(json.dumps(summary, indent=2))
    if summary["status"] != "automated_checks_passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
