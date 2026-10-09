"""Plan and safely create editable projects from packaged starter resources."""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

TEMPLATES = ("demo", "web", "windows", "inventory")


def template_files(template: str) -> dict[str, bytes]:
    """Read one resource source, including dotfiles, without needing a checkout."""
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}")
    result: dict[str, bytes] = {}

    def visit(directory: Traversable, prefix: str = "") -> None:
        for item in sorted(directory.iterdir(), key=lambda item: item.name):
            name = prefix + item.name
            if item.is_dir():
                visit(item, name + "/")
            else:
                result[name] = item.read_bytes()

    visit(files("cua.resources").joinpath("templates", template))
    if template == "demo":
        result[".env"] = result[".env.example"]
    return result


def plan(path: Path, template: str) -> dict[str, bytes]:
    """Preflight every destination before creating a file or generating a key."""
    contents = template_files(template)
    contents[".cua/approval-signing.key"] = b""  # Filled only after preflight, never in dry runs.
    for name in contents:
        target = path / name
        for parent in (target, *target.parents):
            if parent.is_symlink():
                raise ValueError(f"refusing symlink destination: {parent}")
            if parent == target:
                if parent.exists():
                    raise FileExistsError(f"refusing existing file: {parent}")
            elif parent.exists() and not parent.is_dir():
                raise FileExistsError(f"destination parent is not a directory: {parent}")
    return contents


def initialize(path: Path, template: str, *, dry_run: bool = False) -> list[str]:
    """Exclusive writes and rollback preserve existing files even on a failed write."""
    path = path.absolute()
    # Check the caller's path before normalizing '..', which could otherwise
    # conceal a symlink ancestor. Missing intermediate directories need not be
    # created when a path such as new/../project selects a sibling directory.
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError(f"refusing symlink destination: {parent}")
    path = path.resolve()
    contents = plan(path, template)
    if dry_run:
        return sorted(contents)
    contents[".cua/approval-signing.key"] = (secrets.token_hex(32) + "\n").encode()
    created: list[Path] = []
    directories: list[Path] = []
    try:
        for name, data in contents.items():
            target = path / name
            missing = [parent for parent in target.parents if not parent.exists()]
            for parent in reversed(missing):
                parent.mkdir()
                directories.append(parent)
            # O_EXCL is also a final collision check if another writer raced preflight.
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append(target)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
    except BaseException:
        for target in reversed(created):
            target.unlink()
        for directory in reversed(directories):
            directory.rmdir()
        raise
    return sorted(contents)


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("init", help="Create an editable project from packaged templates")
    parser.add_argument("path", type=Path, help="Destination; existing files are never overwritten")
    parser.add_argument("--template", choices=TEMPLATES, default="demo")
    parser.add_argument("--dry-run", action="store_true", help="List planned files without writing")


def main(args: argparse.Namespace) -> int:
    try:
        names = initialize(args.path, args.template, dry_run=args.dry_run)
    except (OSError, ValueError) as exc:
        print(f"cua init: {exc}", file=sys.stderr)
        return 64
    action = "Would create" if args.dry_run else "Created"
    print(f"{action} {args.template} project at {args.path}")
    for name in names:
        print(f"  {name}")
    print(f"Next: read {args.path / 'README.md'}")
    return 0
