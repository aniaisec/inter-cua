"""Run with the non-editable wheel's interpreter, outside editable installations."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    with zipfile.ZipFile(args.wheel) as archive:
        names = set(archive.namelist())
        required = {
            "cua/doctor.py",
            "cua/resources/templates/demo/cua.toml",
            "cua/resources/templates/demo/.gitignore",
            "cua/resources/templates/demo/.env.example",
            "cua/resources/templates/demo/capabilities/open_subaccount.json",
            "cua/resources/templates/demo/capabilities/schema/capability-1.3.json",
            "cua/resources/templates/demo/bench/security/scripts/external_link.yaml",
            "cua/resources/templates/windows/deskapp/deskcalc.ps1",
            "cua/resources/templates/web/capabilities/families/my-web-app.yaml",
            "mockapp/templates/login.html",
        }
        assert required <= names, f"wheel missing resources: {required - names}"
        assert not any("/reviews/" in name or name.endswith(".key") for name in names)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONUTF8"] = "1"
    with tempfile.TemporaryDirectory(prefix="cua installed with spaces ") as temp:
        cwd = Path(temp)

        def run(*args: str, expected: tuple[int, ...] = (0,)) -> str:
            result = subprocess.run(
                [sys.executable, *args],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=300,
            )
            assert result.returncode in expected, result.stdout + result.stderr
            return result.stdout

        origin = run("-c", "import cua; print(cua.__file__)").strip()
        assert "site-packages" in origin, f"expected installed wheel, got {origin}"
        run("-m", "cua.cli", "--help")
        for template in ("demo", "web", "windows"):
            run("-m", "cua.cli", "init", template, "--template", template)
            run("-m", "cua.cli", "--root", template, "surfaces")
            run(
                "-c",
                "from pathlib import Path; from cua.project import ProjectContext; "
                "from cua.tenant import load_tenant; "
                "from cua.policy.allowlist import load_policy; "
                "from cua.artifact.recorder import load_family; "
                f"p=ProjectContext.resolve(Path({template!r})); "
                "t=load_tenant('local',project=p); load_policy(Path(t.policy),t); "
                "load_family(t.app_family,p.path('families'))",
            )
        run(
            "-c",
            "from fastapi.testclient import TestClient; from mockapp.app import app; "
            "r=TestClient(app).get('/login'); assert r.status_code==200; "
            "assert 'Password' in r.text",
        )
        diagnostic = json.loads(
            run("-m", "cua.cli", "--root", "demo", "doctor", "--json", expected=(0, 1))
        )
        assert diagnostic["report_version"] == 1
        assert "PROJECT_OK" in {f["code"] for f in diagnostic["findings"]}
        key = (cwd / "demo/.cua/approval-signing.key").read_text().strip()
        assert key not in json.dumps(diagnostic)
        if args.browser:
            diagnostic = json.loads(
                run("-m", "cua.cli", "--root", "demo", "doctor", "--json", "--probe-browser")
            )
            assert diagnostic["ready"]
            assert "BROWSER_LAUNCH_OK" in {f["code"] for f in diagnostic["findings"]}
            run("-m", "cua.cli", "demo", "--out", "session", "--repetitions", "1")
            summary = json.loads((cwd / "session/summary.json").read_text(encoding="utf-8"))
            assert summary["replay_model_calls"] == 0
            assert summary["commits"] == 1
    print("Installed wheel: resources, templates, mock HTML, and requested demo checks passed")


if __name__ == "__main__":
    main()
