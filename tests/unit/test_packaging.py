"""Optional SDK errors precede UI work; distribution gates reject broken releases."""

import builtins
import io
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from cua import __version__
from cua.agent.llm import NoProviderError, select_client
from cua.cli import main
from cua.initialize import initialize
from scripts.ci.check_distribution import RESOURCES, check, check_metadata

ROOT = Path(__file__).resolve().parents[2]
METADATA = """Metadata-Version: 2.4
Name: inter-cua
Version: 0.1.0
Requires-Python: >=3.11
Provides-Extra: anthropic
Provides-Extra: gemini
Provides-Extra: discovery
Provides-Extra: vision
Provides-Extra: windows
Provides-Extra: dev
Requires-Dist: anthropic<1.0,>=0.40; extra == 'anthropic'
Requires-Dist: google-genai<3.0,>=2.0; extra == 'gemini'
Project-URL: Repository, https://github.com/aniaisec/inter-cua
Classifier: Programming Language :: Python :: 3.11
"""


def block_sdks(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "anthropic" or name == "google" or name.startswith("google.genai"):
            raise ModuleNotFoundError(f"blocked SDK: {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)


@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_missing_sdk_has_remedy_before_discovery_launch(tmp_path, monkeypatch, capsys, provider):
    root = tmp_path / "project"
    initialize(root, "demo")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-not-a-key")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-not-a-key")
    block_sdks(monkeypatch)
    with patch(
        "cua.surface.playwright_surface.PlaywrightSurface", side_effect=AssertionError("UI")
    ):
        result = main(
            [
                "--root",
                str(root),
                "discover",
                "--goal",
                "Look up a member",
                "--entry",
                "/login",
                "--llm",
                provider,
            ]
        )
    assert result == 64
    assert f'python -m pip install "inter-cua[{provider}]"' in capsys.readouterr().err
    assert not (root / "evidence").exists()


def test_auto_preserves_preference_when_sdk_missing(monkeypatch):
    block_sdks(monkeypatch)
    with pytest.raises(NoProviderError, match=r"inter-cua\[anthropic\]"):
        select_client("auto", environ={"ANTHROPIC_API_KEY": "fake", "GEMINI_API_KEY": "fake"})


def test_key_error_still_precedes_sdk_import(monkeypatch):
    block_sdks(monkeypatch)
    with pytest.raises(NoProviderError, match="ANTHROPIC_API_KEY"):
        select_client("anthropic", environ={})


def test_cli_version_uses_package_source(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"cua {__version__}"


def test_cli_and_provider_module_import_without_sdk(tmp_path):
    env = {
        key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME")
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
class RejectSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'anthropic' or fullname.startswith(('anthropic.', 'google.genai')):
            raise AssertionError('eager SDK import: ' + fullname)
sys.meta_path.insert(0, RejectSDK())
import cua.cli
import cua.agent.llm
""",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "broken",
    [
        METADATA.replace("; extra == 'anthropic'", ""),
        METADATA.replace("; extra == 'gemini'", "; extra == 'vision'"),
        METADATA.replace("Requires-Dist: anthropic<1.0,>=0.40; extra == 'anthropic'\n", ""),
        METADATA.replace("Provides-Extra: vision\n", ""),
    ],
)
def test_metadata_gate_rejects_provider_or_extra_regressions(broken):
    with pytest.raises(AssertionError):
        check_metadata(broken.encode())


def test_metadata_gate_accepts_hatch_expansion_of_combined_extras():
    expanded = METADATA + (
        "Requires-Dist: anthropic>=0.40; extra == 'dev'\n"
        "Requires-Dist: google-genai>=2.0; extra == 'discovery'\n"
    )
    assert check_metadata(expanded.encode()) == "0.1.0"


@pytest.mark.parametrize("fault", [None, "wheel-resource", "sdist-resource", "version", "secret"])
def test_archive_gate_checks_resources_version_and_private_files(tmp_path, fault):
    wheel = tmp_path / "inter_cua-0.1.0-py3-none-any.whl"
    sdist = tmp_path / "inter_cua-0.1.0.tar.gz"
    wheel_files = dict.fromkeys(RESOURCES, b"fixture")
    wheel_files["inter_cua-0.1.0.dist-info/METADATA"] = METADATA.encode()
    sdist_files = {
        "src/" + name if name.startswith("cua/") else name: b"fixture" for name in RESOURCES
    }
    sdist_files.update(
        {
            "pyproject.toml": (ROOT / "pyproject.toml").read_bytes(),
            "src/cua/__init__.py": b'__version__ = "0.1.0"',
            "CHANGELOG.md": b"changelog",
            "PKG-INFO": METADATA.encode(),
        }
    )
    if fault == "wheel-resource":
        del wheel_files["mockapp/templates/login.html"]
    elif fault == "sdist-resource":
        del sdist_files["src/cua/resources/templates/demo/.env.example"]
    elif fault == "version":
        sdist_files["src/cua/__init__.py"] = b'__version__ = "9.9.9"'
    elif fault == "secret":
        sdist_files["src/cua/resources/templates/demo/signing.key"] = b"secret"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in wheel_files.items():
            archive.writestr(name, data)
    with tarfile.open(sdist, "w:gz") as archive:
        for name, data in sdist_files.items():
            member = tarfile.TarInfo("inter_cua-0.1.0/" + name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    if fault is None:
        check(wheel, sdist)
    else:
        with pytest.raises(AssertionError):
            check(wheel, sdist)
