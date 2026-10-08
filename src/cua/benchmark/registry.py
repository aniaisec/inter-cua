"""Load benchmark suites from ``bench/tasks/`` and pick tasks out of them.

A suite is one file (``bench/tasks/core.yaml``, the first suite), or one
category directory (``bench/tasks/<category>/*.yaml``), whose tasks all carry
the category. ``all`` is every category directory together.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import ValidationError

from cua.benchmark.models import BenchmarkTask, Suite
from cua.project import ProjectContext

TASKS_DIR = Path("bench/tasks")
ALL = "all"


class SuiteError(ValueError):
    pass


def suite_path(name_or_path: str, *, root: Path = TASKS_DIR) -> Path:
    path = Path(name_or_path)
    if path.suffix in (".yaml", ".yml") or path.is_dir():
        return path
    if (root / name_or_path).is_dir():
        return root / name_or_path
    return root / f"{name_or_path}.yaml"


def categories(root: Path = TASKS_DIR) -> list[str]:
    """The category directories under ``root``, by name."""
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and any(p.glob("*.yaml")))


def load_suite(
    name_or_path: str, *, root: Path = TASKS_DIR, project: ProjectContext | None = None
) -> Suite:
    suite = _load_suite(name_or_path, root=root)
    if project is None:
        return suite
    tasks = []
    for task in suite.tasks:
        bound = task.model_copy(
            update={
                "capability": str(project.relative(task.capability)) if task.capability else None,
                "workflow": str(project.relative(task.workflow)) if task.workflow else None,
                "goal": task.goal.model_copy(
                    update={"script": str(project.relative(task.goal.script))}
                )
                if task.goal.script
                else task.goal,
            }
        )
        bound._declared_paths = {
            name: (str(project.relative(value)), value)
            for name, value in (
                ("capability", task.capability),
                ("workflow", task.workflow),
                ("script", task.goal.script),
            )
            if value is not None
        }
        tasks.append(bound)
    return suite.model_copy(update={"tasks": tasks})


def _load_suite(name_or_path: str, *, root: Path = TASKS_DIR) -> Suite:
    """``core`` → ``bench/tasks/core.yaml``; ``browser`` → every file in
    ``bench/tasks/browser/``; ``all`` → every category; a path as it is."""
    if name_or_path == ALL:
        parts = [load_suite(c, root=root) for c in categories(root)]
        if not parts:
            raise SuiteError(f"no category directories under {root.as_posix()}")
        return _merged(ALL, "Every category suite.", parts, root)
    path = suite_path(name_or_path, root=root)
    if path.is_dir():
        files = sorted(path.glob("*.yaml"))
        if not files:
            raise SuiteError(f"no task files in {path.as_posix()}")
        parts = [_file(f, category=path.name) for f in files]
        return _merged(path.name, parts[0].description, parts, path)
    return _file(path)


def _file(path: Path, *, category: str | None = None) -> Suite:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if category is not None:
            for task in data.get("tasks") or []:
                task.setdefault("category", category)
                tags = task.setdefault("tags", [])
                if category not in tags:
                    tags.insert(0, category)
        suite = Suite.model_validate(data)
    except OSError as exc:
        raise SuiteError(f"cannot read suite {path.as_posix()}: {exc}") from None
    except (yaml.YAMLError, ValidationError, AttributeError) as exc:
        raise SuiteError(f"{path.as_posix()} is not a valid suite: {exc}") from None
    return suite.model_copy(update={"path": path})


def _merged(name: str, description: str, parts: list[Suite], path: Path) -> Suite:
    try:
        suite = Suite(name=name, description=description, tasks=[t for p in parts for t in p.tasks])
    except ValidationError as exc:
        raise SuiteError(f"suite {name} is not valid: {exc}") from None
    return suite.model_copy(update={"path": path})


def select(
    suite: Suite,
    *,
    task_ids: Iterable[str] = (),
    tags: Iterable[str] = (),
    include_manual: bool = False,
) -> list[BenchmarkTask]:
    """The suite's tasks, narrowed to ``task_ids`` and to any of ``tags``.

    An id that is not in the suite is an error, not an empty result: a typo
    must not turn into a benchmark that quietly ran nothing."""
    wanted = list(task_ids)
    unknown = sorted(set(wanted) - {t.id for t in suite.tasks})
    if unknown:
        raise SuiteError(f"no task {', '.join(unknown)} in suite {suite.name}")
    tag_set = set(tags)
    chosen = [
        t
        for t in suite.tasks
        if (not wanted or t.id in wanted)
        and (not tag_set or tag_set & set(t.tags))
        and (t.automated or include_manual)
    ]
    return chosen
