"""Validate release archives and metadata without importing the source checkout."""

from __future__ import annotations

import argparse
import re
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

RESOURCES = {
    "cua/resources/templates/demo/cua.toml",
    "cua/resources/templates/demo/.gitignore",
    "cua/resources/templates/demo/.env.example",
    "cua/resources/templates/demo/capabilities/open_subaccount.json",
    "cua/resources/templates/demo/capabilities/schema/capability-1.3.json",
    "cua/resources/templates/demo/bench/security/scripts/external_link.yaml",
    "cua/resources/templates/web/capabilities/families/my-web-app.yaml",
    "cua/resources/templates/windows/deskapp/deskcalc.ps1",
    "mockapp/templates/login.html",
    "inventoryapp/app.py",
    "inventoryapp/templates/screen.html",
    "cua/resources/templates/inventory/.env.example",
    "cua/resources/templates/inventory/README.md",
    "cua/resources/templates/inventory/capabilities/families/stockroom.yaml",
    "cua/resources/templates/inventory/scripts/discovery/adjust_stock.yaml",
    "cua/resources/templates/inventory/scenario.json",
}


def source_path(name: str) -> str:
    if name.startswith("inventoryapp/"):
        return "examples/inventory/app/" + name.removeprefix("inventoryapp/")
    return "src/" + name if name.startswith("cua/") else name


def check_metadata(data: bytes) -> str:
    message = BytesParser().parsebytes(data)
    assert message["Name"] == "inter-cua"
    assert message["Requires-Python"] == ">=3.11"
    extras = set(message.get_all("Provides-Extra", []))
    assert {"anthropic", "gemini", "discovery", "vision", "windows", "dev"} <= extras
    providers = set()
    for requirement in message.get_all("Requires-Dist", []):
        name = re.split(r"[\[<>=!~\s]", requirement, maxsplit=1)[0]
        if name in ("anthropic", "google-genai"):
            extra = "anthropic" if name == "anthropic" else "gemini"
            marker = requirement.partition(";")[2].strip()
            # Hatch expands self-referencing discovery/dev extras in the archives.
            allowed = {
                f"extra == {quote}{tag}{quote}"
                for tag in (extra, "discovery", "dev")
                for quote in ("'", '"')
            }
            assert marker in allowed, requirement
            if marker in (f"extra == '{extra}'", f'extra == "{extra}"'):
                providers.add(name)
    assert providers == {"anthropic", "google-genai"}, f"missing provider extras: {providers}"
    assert any("Repository" in item for item in message.get_all("Project-URL", []))
    assert "Programming Language :: Python :: 3.11" in message.get_all("Classifier", [])
    return str(message["Version"])


def check(wheel: Path, sdist: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert RESOURCES <= names, f"wheel missing: {RESOURCES - names}"
        assert not any(name.endswith(".key") or "/reviews/" in name for name in names)
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        assert len(metadata) == 1
        wheel_version = check_metadata(archive.read(metadata[0]))
    with tarfile.open(sdist, "r:gz") as archive:
        members = {member.name.partition("/")[2]: member for member in archive.getmembers()}
        required = {source_path(name) for name in RESOURCES}
        required.update({"pyproject.toml", "src/cua/__init__.py", "CHANGELOG.md", "PKG-INFO"})
        assert required <= members.keys(), f"sdist missing: {required - members.keys()}"
        assert not any(name.endswith(".key") or "/reviews/" in name for name in members)

        def read(name: str) -> bytes:
            stream = archive.extractfile(members[name])
            assert stream is not None
            with stream:
                return stream.read()

        sdist_version = check_metadata(read("PKG-INFO"))
        project = tomllib.loads(read("pyproject.toml").decode("utf-8"))
        assert "version" not in project["project"]
        assert "version" in project["project"]["dynamic"]
        assert project["tool"]["hatch"]["version"]["path"] == "src/cua/__init__.py"
        source_version = re.search(rb'__version__ = "([^"]+)"', read("src/cua/__init__.py"))
        assert source_version is not None
        assert wheel_version == sdist_version == source_version[1].decode("utf-8")
    print(f"Distribution {wheel_version}: wheel/sdist metadata and resources passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("sdist", type=Path)
    args = parser.parse_args()
    check(args.wheel, args.sdist)


if __name__ == "__main__":
    main()
