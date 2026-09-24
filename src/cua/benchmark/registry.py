"""Load benchmark suites from ``bench/tasks/*.yaml`` and pick tasks out of them."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import ValidationError

from cua.benchmark.models import BenchmarkTask, Suite

TASKS_DIR = Path("bench/tasks")


class SuiteError(ValueError):
    pass


def suite_path(name_or_path: str, *, root: Path = TASKS_DIR) -> Path:
    path = Path(name_or_path)
    if path.suffix not in (".yaml", ".yml"):
        path = root / f"{name_or_path}.yaml"
    return path


def load_suite(name_or_path: str, *, root: Path = TASKS_DIR) -> Suite:
    """``core`` → ``bench/tasks/core.yaml``; a path is taken as it is."""
    path = suite_path(name_or_path, root=root)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        suite = Suite.model_validate(data)
    except OSError as exc:
        raise SuiteError(f"cannot read suite {path.as_posix()}: {exc}") from None
    except (yaml.YAMLError, ValidationError) as exc:
        raise SuiteError(f"{path.as_posix()} is not a valid suite: {exc}") from None
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
