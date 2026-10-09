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
    parser.add_argument(
        "--extra",
        choices=("base", "anthropic", "gemini", "discovery", "vision", "windows"),
        default="base",
    )
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
    env.pop("PYTHONHOME", None)
    # Installed tests use synthetic inputs and must not inherit provider auth/endpoints.
    for key in list(env):
        if key.startswith(("ANTHROPIC_", "GEMINI_", "GOOGLE_")):
            env.pop(key)
    env["PYTHONUTF8"] = "1"
    with tempfile.TemporaryDirectory(prefix="cua installed with spaces ") as temp:
        cwd = Path(temp)

        def run(*args: str, expected: tuple[int, ...] = (0,), console: bool = False) -> str:
            executable = (
                str(Path(sys.executable).with_name("cua.exe" if os.name == "nt" else "cua"))
                if console
                else sys.executable
            )
            result = subprocess.run(
                [executable, *args],
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
        run("--help", console=True)
        installed_version = run(
            "-c",
            "from importlib.metadata import version; import cua; "
            "assert cua.__version__ == version('inter-cua'); print(cua.__version__)",
        ).strip()
        assert run("-m", "cua.cli", "--version").strip() == f"cua {installed_version}"
        assert run("--version", console=True).strip() == f"cua {installed_version}"
        run(
            "-c",
            "import sys; import cua.cli; from cua.agent import llm; "
            "assert not any(m == 'anthropic' or m.startswith(('anthropic.', 'google.genai')) "
            "for m in sys.modules), 'eager provider import'",
        )
        providers = {
            "base": [],
            "anthropic": ["anthropic"],
            "gemini": ["gemini"],
            "discovery": ["anthropic", "gemini"],
            "vision": [],
            "windows": [],
        }[args.extra]
        run(
            "-c",
            "from importlib import metadata; from unittest.mock import patch; "
            "from cua.agent.llm import select_client, NoProviderError; "
            f"expected = {providers!r}\n"
            "for provider, distribution in "
            "[('anthropic', 'anthropic'), ('gemini', 'google-genai')]:\n"
            "    try:\n"
            "        metadata.version(distribution)\n"
            "        present = True\n"
            "    except metadata.PackageNotFoundError:\n"
            "        present = False\n"
            "    assert present == (provider in expected), (provider, present, expected)\n"
            "    with patch('socket.socket.connect', side_effect=AssertionError('network call')):\n"
            "        try:\n"
            "            client = select_client(provider, environ={\n"
            "                'ANTHROPIC_API_KEY': 'synthetic-not-a-key',\n"
            "                'GEMINI_API_KEY': 'synthetic-not-a-key'})\n"
            "        except NoProviderError as exc:\n"
            "            assert provider not in expected\n"
            "            assert f'inter-cua[{provider}]' in str(exc), str(exc)\n"
            "        else:\n"
            "            assert provider in expected and client.provider == provider\n"
            "            client._client.close()\n",
        )
        if args.extra == "vision":
            run("-c", "from cua.surface.vision.image import available; assert available()")
        elif args.extra == "windows":
            run(
                "-c",
                "import sys; assert sys.platform == 'win32'; import comtypes; "
                "from cua.surface.windows.adapter import WindowsSurface; "
                "assert WindowsSurface.DESCRIPTOR.name == 'windows-uia'",
            )
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
    print(f"Installed wheel ({args.extra}): version, extras, resources, and requested demo passed")


if __name__ == "__main__":
    main()
