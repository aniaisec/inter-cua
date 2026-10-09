"""Record Python 3.11 development constraints from a freshly resolved dev environment."""

from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 11):
        parser.error("generate the development snapshot with Python 3.11")
    pins = {}
    for distribution in metadata.distributions():
        name = re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower()
        if name != "inter-cua":
            pins[name] = distribution.version
    for name in (
        "anthropic",
        "google-genai",
        "numpy",
        "pillow",
        "build",
        "hatchling",
        "pytest",
        "mypy",
        "ruff",
    ):
        if name not in pins:
            parser.error(f"install .[dev] first: missing {name}")
    lines = [
        "# Python 3.11 development snapshot. Apply with -c, not -r.",
        "# Regenerate in a fresh environment with scripts/ci/lock_dev.py; see docs/packaging.md.",
    ]
    lines += [f"{name}=={version}" for name, version in sorted(pins.items())]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Recorded {len(pins)} development constraints")


if __name__ == "__main__":
    main()
