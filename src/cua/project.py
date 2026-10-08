"""Project selection and absolute paths, independent of process cwd.

Version 1 keeps configuration references relative to the project, while
explicit command-line files stay relative to the invocation directory.
"""

from __future__ import annotations

import argparse
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ProjectPaths(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenants: str = "tenants"
    capabilities: str = "capabilities"
    families: str = "capabilities/families"
    workflows: str = "workflows"
    runs: str = "evidence/runs"
    state: str = ".cua"
    access: str = "api/access.yaml"


class ProjectConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    version: int
    default_tenant: str = Field(default="local", min_length=1)
    paths: ProjectPaths = Field(default_factory=ProjectPaths)


@dataclass(frozen=True)
class ProjectContext:
    root: Path
    invocation_dir: Path
    config: ProjectConfig
    configured: bool = False
    environ: dict[str, str] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def resolve(
        cls,
        root: Path | None = None,
        *,
        cwd: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> ProjectContext:
        invocation = (cwd or Path.cwd()).resolve()
        selected = (invocation / root).resolve() if root is not None else invocation
        if root is not None and not selected.is_dir():
            raise ValueError(f"project root is not a directory: {selected}")
        if root is None:
            for ancestor in (invocation, *invocation.parents):
                if (ancestor / "cua.toml").exists():
                    selected = ancestor
                    break
        path = selected / "cua.toml"
        configured = path.exists()
        try:
            config = (
                ProjectConfig.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
                if configured
                else ProjectConfig(version=1)
            )
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid project configuration at {path}: {exc}") from exc
        if config.version != 1:
            raise ValueError(f"unsupported cua.toml version {config.version}; expected 1")
        for name, value in config.paths.model_dump().items():
            if not value.strip():
                raise ValueError(f"project path {name} must not be empty")
        env = dotenv(selected / ".env")
        env.update(os.environ if environ is None else environ)
        return cls(selected, invocation, config, configured, env)

    def path(self, name: str) -> Path:
        return self.relative(getattr(self.config.paths, name))

    def relative(self, path: str | Path) -> Path:
        """A configuration reference, relative to the selected project."""
        return (self.root / path).resolve()

    def explicit(self, path: str | Path) -> Path:
        """A CLI file argument, relative to where the caller invoked us."""
        return (self.invocation_dir / path).resolve()


def dotenv(path: Path) -> dict[str, str]:
    """Read the existing simple KEY=VALUE syntax without mutating os.environ."""
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key and value:
                values.setdefault(key, value)
    return values


def bind_cli(parser: argparse.ArgumentParser, args: argparse.Namespace, argv: list[str]) -> None:
    """Resolve parsed paths once. Parser actions distinguish defaults from CLI files."""
    project: ProjectContext = args.project
    actions: list[argparse.Action] = []

    def collect(current: argparse.ArgumentParser) -> None:
        for action in current._actions:
            actions.append(action)
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    collect(child)

    collect(parser)
    saved = [(action, action.default) for action in actions]
    try:
        for action, _ in saved:
            action.default = argparse.SUPPRESS
        explicit_args = vars(parser.parse_args(argv))
    finally:
        for action, default in saved:
            action.default = default
    defaults = {
        "tenants_dir": "tenants",
        "capabilities_dir": "capabilities",
        "families_dir": "families",
        "workflows_dir": "workflows",
        "runs_dir": "runs",
        "state_dir": "state",
        "access": "access",
    }

    def visit(current: argparse.ArgumentParser) -> None:
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                chosen = getattr(args, action.dest, None)
                if chosen in action.choices:
                    visit(action.choices[chosen])
                continue
            if action.dest in ("root", "project_root") or not hasattr(args, action.dest):
                continue
            value = getattr(args, action.dest)
            explicit = action.dest in explicit_args
            if action.type is Path and value is not None:
                if isinstance(value, list):
                    value = [project.explicit(p) for p in value]
                    if not value and action.dest == "runs_dir":
                        value = [
                            project.path("runs"),
                            project.relative("evidence"),
                            project.relative("bench/runs"),
                        ]
                elif explicit:
                    value = project.explicit(value)
                elif action.dest in defaults:
                    value = project.path(defaults[action.dest])
                elif args.command == "schema" and action.dest == "out":
                    value = project.path("capabilities") / "schema" / value.name
                else:
                    value = project.relative(value)
                setattr(args, action.dest, value)
            if action.dest == "tenant" and not explicit and value == "local":
                args.tenant = project.config.default_tenant
            elif action.dest == "tenant" and value and Path(value).suffix in (".yaml", ".yml"):
                args.tenant = str(project.explicit(value))

    visit(parser)
    for name in ("workflow", "suite", "run", "request"):
        value = getattr(args, name, None)
        if (
            isinstance(value, str)
            and value != "-"
            and (Path(value).suffix in (".yaml", ".yml", ".json") or "/" in value or "\\" in value)
        ):
            setattr(args, name, str(project.explicit(value)))
    # Positional capabilities can also be named, just like catalog tools.
    if hasattr(args, "capability") and isinstance(args.capability, Path):
        original = explicit_args["capability"]
        if len(original.parts) == 1 and not original.suffix:
            from cua.catalog import find

            working = project.path("capabilities") / f"{original}.json"
            args.capability = (
                working
                if args.command != "replay" and working.is_file()
                else (find(project.path("capabilities"), str(original), None, project=project))
            )
