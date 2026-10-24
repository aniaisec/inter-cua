"""Build an exact committed snapshot and prepare private PR11 qualification evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from scripts.ci.check_adoption_release import GATES, Evidence, Gate, Release


def evidence(path: Path, root: Path) -> dict[str, str]:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": path.relative_to(root).as_posix(), "sha256": digest}


def extract_snapshot(archive: Path, target: Path) -> None:
    """A build snapshot must contain only files/directories inside its root."""
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            path = PurePosixPath(member.name)
            if (
                not (member.isfile() or member.isdir())
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in member.name
                or ":" in member.name
            ):
                raise ValueError(f"unsupported snapshot member: {member.name}")
        source.extractall(target, filter="data")


def prepare(
    root: Path, out: Path, commit: str, *, verify_installed: bool = False
) -> dict[str, object]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("pass the full reviewed 40-character commit ID")
    resolved = subprocess.check_output(
        ["git", "--no-replace-objects", "rev-parse", "--verify", f"{commit}^{{commit}}"],
        cwd=root,
        text=True,
    ).strip()
    if resolved != commit:
        raise ValueError("candidate commit does not resolve to the requested identity")
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    provenance: dict[str, object] = {
        "schema_version": 1,
        "commit": commit,
        "created_at": datetime.now(UTC).isoformat(),
        "environment": f"{platform.platform()} / Python {platform.python_version()}",
        "commands": [],
        "checks": {},
    }
    commands: list[list[str]] = []
    checks: dict[str, str] = {}
    provenance["commands"] = commands
    provenance["checks"] = checks
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONUTF8"] = "1"
    summary: dict[str, object] = {"status": "failed", "release_status": "not_qualified"}

    def run(name: str, command: list[str], cwd: Path) -> None:
        commands.append(command)
        with (out / f"{name}.log").open("w", encoding="utf-8") as log:
            try:
                subprocess.run(
                    command,
                    cwd=cwd,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                    timeout=1800 if name.startswith("installed") else 300,
                )
            except (OSError, subprocess.SubprocessError):
                checks[name] = "failed"
                raise
        checks[name] = "passed"

    try:
        archive = out / "source.tar"
        run(
            "snapshot",
            [
                "git",
                "--no-replace-objects",
                "archive",
                "--format=tar",
                f"--output={archive}",
                commit,
            ],
            root,
        )
        provenance["source"] = evidence(archive, out)
        with tempfile.TemporaryDirectory(prefix="cua candidate snapshot ") as workspace:
            source = Path(workspace) / "source"
            source.mkdir()
            extract_snapshot(archive, source)
            dist = out / "dist"
            run(
                "build",
                [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(dist)],
                source,
            )
            wheels, sdists = list(dist.glob("*.whl")), list(dist.glob("*.tar.gz"))
            if len(wheels) != 1 or len(sdists) != 1:
                raise ValueError("build must produce exactly one wheel and one sdist")
            wheel, sdist = wheels[0], sdists[0]
            helpers = source / "scripts" / "ci"
            run(
                "distribution",
                [sys.executable, str(helpers / "check_distribution.py"), str(wheel), str(sdist)],
                source,
            )
            with zipfile.ZipFile(wheel) as package:
                paths = [n for n in package.namelist() if n.endswith(".dist-info/METADATA")]
                if len(paths) != 1:
                    raise ValueError("wheel has no unique package metadata")
                metadata = BytesParser().parsebytes(package.read(paths[0]))
            version = str(metadata["Version"])
            if metadata["Name"] != "inter-cua" or not version or version == "None":
                raise ValueError("wheel package identity is invalid")
            provenance["wheel"] = evidence(wheel, out)
            provenance["sdist"] = evidence(sdist, out)
            record = Release(
                schema_version=1,
                version=version,
                commit=commit,
                wheel=Evidence(**evidence(wheel, out)),
                gates={name: Gate(status="pending") for name in GATES},
                trials=[],
                open_blockers=[
                    "Candidate gate review and two independent developer trials remain pending."
                ],
            )
            (out / "record.json").write_text(
                record.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            trials = out / "trials"
            trials.mkdir()
            for participant in ("A", "B"):
                shutil.copyfile(
                    source / "docs/release/trial-template.md",
                    trials / f"participant-{participant}.md",
                )
            if verify_installed:
                for name, flags in (("installed-wheel", []), ("installed-sdist", ["--from-sdist"])):
                    run(
                        name,
                        [
                            sys.executable,
                            str(helpers / "verify_installed.py"),
                            str(dist),
                            "--browser",
                            *flags,
                        ],
                        source,
                    )
            summary.update(
                status="candidate_prepared",
                commit=commit,
                version=version,
                record=str(out / "record.json"),
            )
        return summary
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        (out / "provenance.json").write_text(
            json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
        )
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True, help="Full reviewed source commit ID")
    parser.add_argument("--out", type=Path, required=True, help="New private evidence directory")
    parser.add_argument(
        "--verify-installed", action="store_true", help="Also run fresh wheel/sdist browser checks"
    )
    args = parser.parse_args()
    try:
        result = prepare(
            Path(__file__).resolve().parents[2],
            args.out,
            args.commit,
            verify_installed=args.verify_installed,
        )
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        parser.exit(1, f"adoption candidate preparation failed: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
