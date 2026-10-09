"""Run with the non-editable wheel's interpreter, outside editable installations."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cua.demo.runner import server


@contextmanager
def temporary_workspace() -> Iterator[Path]:
    """Remove our own scratch project after Windows releases process handles."""
    workspace = tempfile.TemporaryDirectory(prefix="cua installed with spaces ")
    try:
        yield Path(workspace.name)
    finally:
        deadline = time.monotonic() + 5
        while True:
            try:
                workspace.cleanup()
                break
            except PermissionError as exc:
                if (
                    sys.platform != "win32"
                    or getattr(exc, "winerror", None) not in (5, 32)
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(0.1)


def check_inventory(cwd: Path, env: dict[str, str]) -> None:
    """Use the installed CLI against the packaged second target, outside the repo."""
    import httpx
    import yaml

    root = cwd / "inventory"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"

    def cli(*args: str, expected: int = 0) -> str:
        result = subprocess.run(
            [sys.executable, "-m", "cua.cli", "--root", str(root), *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        return result.stdout

    def oracle() -> dict[str, object]:
        response = httpx.get(url + "/_test/state", timeout=5)
        response.raise_for_status()
        return dict(response.json())

    def replay_result(name: str, *args: str, expected: int = 0) -> dict[str, object]:
        result = json.loads(cli("replay", name, *args, expected=expected))
        run_dir = result["evidence"]["run_dir"]
        if run_dir:
            assert not (root / run_dir / "model_calls.jsonl").exists()
        return dict(result)

    with server(
        [
            "uvicorn",
            "inventoryapp.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        url + "/login",
        cwd,
        env,
    ):
        tenant_path = root / "tenants/local.yaml"
        tenant = yaml.safe_load(tenant_path.read_text())
        tenant["base_url"] = url
        tenant_path.write_text(yaml.safe_dump(tenant))
        (root / ".env").write_bytes((root / ".env.example").read_bytes())
        assert not list((root / "capabilities").glob("*.json"))
        scenario = json.loads((root / "scenario.json").read_text())
        for name, spec in scenario.items():
            command = [
                "discover",
                "--name",
                name,
                "--entry",
                "/login",
                "--goal",
                spec["goal"],
                "--llm",
                "scripted",
                "--script",
                str(root / "scripts/discovery" / f"{name}.yaml"),
            ]
            for param in spec["params"]:
                command.extend(["--param", param])
            for output in spec["outputs"]:
                command.extend(["--output", output])
            if name == "adjust_stock":
                command.append("--auto-approve-risky")
            cli(*command)
            draft = json.loads((root / "capabilities" / f"{name}.json").read_text())
            assert draft["approval_state"] == "draft" and draft.get("approved_by") is None
            assert (
                replay_result(name, "--input", "item_code=SKU-001", expected=1)["code"]
                == "POLICY_BLOCKED"
            )
            cli("describe", name)
            cli("approve", name, "--by", "installed inventory check")
        lookup = replay_result("item_lookup", "--input", "item_code=SKU-002")
        assert lookup["outputs"] == scenario["item_lookup"]["second_outputs"]
        missing = replay_result("item_lookup", "--input", "item_code=SKU-999", expected=2)
        assert missing["code"] == "NOT_FOUND"
        before = oracle()
        scarce = replay_result(
            "adjust_stock",
            "--input",
            "item_code=SKU-001",
            "--input",
            "quantity_change=-100",
            expected=2,
        )
        assert scarce["code"] == "INSUFFICIENT_STOCK"
        denied = replay_result(
            "adjust_stock",
            "--input",
            "item_code=SKU-001",
            "--input",
            "quantity_change=-3",
            expected=1,
        )
        assert denied["escalation_reason"] == "NEEDS_APPROVAL" and oracle() == before
        inputs = ["--input", "item_code=SKU-001", "--input", "quantity_change=-3"]
        token = cli(
            "approval-token", "adjust_stock", *inputs, "--by", "installed inventory check"
        ).strip()
        args = [
            *inputs,
            "--approval-token",
            token,
            "--idempotency-key",
            "installed-inventory-write",
        ]
        write = replay_result("adjust_stock", *args)
        assert write["side_effect"] == "committed" and write["outputs"] == {
            "quantity": 7,
            "receipt": "ADJ-0002",
        }
        retry = replay_result("adjust_stock", *args)
        assert retry["cached"] and retry["run_id"] == write["run_id"]
        state = oracle()
        assert state["quantities"] == {"SKU-001": 7, "SKU-002": 4}
        assert state["commit_count"] == state["commit_posts"] == 2
        assert len(list((root / "evidence/runs").glob("run_*/model_calls.jsonl"))) == 2


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
            "inventoryapp/app.py",
            "inventoryapp/templates/screen.html",
            "cua/resources/templates/inventory/scenario.json",
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
    with temporary_workspace() as cwd:

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
        for template in ("demo", "web", "windows", "inventory"):
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
        run(
            "-c",
            "from fastapi.testclient import TestClient; from inventoryapp.app import app; "
            "r=TestClient(app).get('/login'); assert r.status_code==200; "
            "assert 'Inventory sign in' in r.text",
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
            check_inventory(cwd, env)
    print(f"Installed wheel ({args.extra}): version, extras, resources, and requested demo passed")


if __name__ == "__main__":
    main()
