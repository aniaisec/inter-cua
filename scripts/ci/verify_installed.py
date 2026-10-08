"""Create a fresh wheel environment and execute installed checks outside the checkout."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist_dir", type=Path)
    parser.add_argument(
        "--extra",
        choices=("base", "anthropic", "gemini", "discovery", "vision", "windows"),
        default="base",
    )
    parser.add_argument("--from-sdist", action="store_true")
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    dist = args.dist_dir.resolve()
    scripts = Path(__file__).resolve().parent
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONUTF8"] = "1"

    def run(command: list[str], cwd: Path) -> None:
        subprocess.run(command, cwd=cwd, env=env, check=True, timeout=600)

    with tempfile.TemporaryDirectory(prefix="cua wheel check ") as temp:
        cwd = Path(temp).resolve()
        if args.from_sdist:
            archives = list(dist.glob("*.tar.gz"))
            assert len(archives) == 1, f"expected one sdist, got {archives}"
            run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-cache-dir",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(cwd / "rebuilt"),
                    str(archives[0]),
                ],
                cwd,
            )
            wheels = list((cwd / "rebuilt").glob("*.whl"))
        else:
            wheels = list(dist.glob("*.whl"))
        assert len(wheels) == 1, f"expected one wheel, got {wheels}"
        wheel = wheels[0]
        environment = cwd / "env"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        requirement = str(wheel) + (f"[{args.extra}]" if args.extra != "base" else "")
        run([str(python), "-m", "pip", "install", "--disable-pip-version-check", requirement], cwd)
        if args.browser:
            run([str(python), "-m", "playwright", "install", "chromium"], cwd)
        command = [
            str(python),
            str(scripts / "check_installed.py"),
            str(wheel),
            "--extra",
            args.extra,
        ]
        if args.browser:
            command.append("--browser")
        run(command, cwd)
    print(f"Fresh {'sdist-built' if args.from_sdist else 'wheel'} environment: {args.extra} passed")


if __name__ == "__main__":
    main()
