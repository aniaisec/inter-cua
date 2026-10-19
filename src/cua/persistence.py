"""Local file integrity, not durable invocation ownership or multi-file transactions.

Writers stage a unique sibling, flush it, and replace the destination. Sidecar
locks serialize cooperating writers across threads/processes; the OS releases a
lock on process death. Never unlink a lock file or steal one based on its age.
"""

from __future__ import annotations

import errno
import json
import math
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar
from weakref import WeakValueDictionary

from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)
LOCK_TIMEOUT_S = 10.0


class PersistenceError(ValueError):
    """An authoritative file cannot be trusted. Never treat it as absent."""


class PersistenceConflict(ValueError):
    """An update was based on an older snapshot; it must not overwrite newer state."""


class _Guard:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.depth = 0


_guards: WeakValueDictionary[str, _Guard] = WeakValueDictionary()
_guards_lock = threading.Lock()


def _os_lock(fd: int, *, unlock: bool = False) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def file_lock(path: Path, *, timeout: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Reentrant, bounded lock for one file's read/check/write operation.

    ``path.name + '.lock'`` is a persistent, private sidecar. Local filesystems
    only; every writer must cooperate. This is not an execution lease.
    """
    path = path.resolve()
    name = os.path.normcase(str(path))
    with _guards_lock:
        guard = _guards.get(name)
        if guard is None:
            guard = _Guard()
            _guards[name] = guard
    deadline = time.monotonic() + timeout
    if not guard.lock.acquire(timeout=max(0.0, timeout)):
        raise TimeoutError(f"timed out locking {path}")
    try:
        if guard.depth:
            guard.depth += 1
            try:
                yield
            finally:
                guard.depth -= 1
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            while True:
                try:
                    _os_lock(fd)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out locking {path}") from None
                    time.sleep(0.02)
            guard.depth = 1
            try:
                yield
            finally:
                guard.depth = 0
                _os_lock(fd, unlock=True)
        finally:
            os.close(fd)
    finally:
        guard.lock.release()


def _sync(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP):
            raise


def _sync_directory(path: Path) -> None:
    # Windows does not expose a directory fsync through os.open/os.fsync.
    if sys.platform != "win32":
        fd = os.open(path, os.O_RDONLY)
        try:
            _sync(fd)
        finally:
            os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    """Publish complete UTF-8/LF text, preserving the old file until replacement.

    No read/modify/write locking is implied. Callers hold ``file_lock`` across
    the entire update when they derive new state from an existing record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    staging = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            _sync(stream.fileno())
        deadline = time.monotonic() + 1.0
        while True:
            try:
                os.replace(staging, path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        _sync_directory(path.parent)
    finally:
        staging.unlink(missing_ok=True)


def atomic_write_json(
    path: Path, data: Any, *, default: Callable[[Any], Any] | None = None
) -> None:
    # Serialize before touching the filesystem; invalid numbers must not be saved.
    text = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False, default=default)
    atomic_write_text(path, text + "\n")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _object(text: str) -> dict[str, Any]:
    value: Any = json.loads(
        text, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_number
    )
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return value


def corrupt(path: Path, *, line: int | None = None) -> PersistenceError:
    where = f" line {line}" if line is not None else ""
    return PersistenceError(
        f"unreadable authoritative state: {path}{where}; stop automation and preserve "
        "the files for reconciliation; see docs/operations.md#corrupt-or-interrupted-state"
    )


def read_json(path: Path) -> dict[str, Any]:
    try:
        return _object(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError):
        raise corrupt(path) from None


def read_model(path: Path, model: type[M]) -> M:
    try:
        return model.model_validate(read_json(path))
    except ValidationError:
        raise corrupt(path) from None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Reject torn tails, duplicate fields and malformed rows, including at EOF."""
    if not path.parent.exists():
        return []
    with file_lock(path):
        return _read_jsonl(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except UnicodeError:
        raise corrupt(path) from None
    if text and not text.endswith("\n"):
        raise corrupt(path, line=len(text.splitlines()))
    records = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():  # legacy logs can contain empty lines
            continue
        try:
            records.append(_object(line))
        except ValueError:
            raise corrupt(path, line=number) from None
    return records


def append_jsonl(
    path: Path, record: dict[str, Any], *, default: Callable[[Any], Any] | None = None
) -> None:
    """Durable, serialized append. Refuse to extend a corrupt authoritative log.

    A crash may leave a partial last row; readers must refuse it rather than
    dropping it. JSONL and its associated snapshot are not one transaction.
    """
    line = json.dumps(record, allow_nan=False, default=default) + "\n"
    with file_lock(path):
        read_jsonl(path)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.flush()
            _sync(stream.fileno())
        _sync_directory(path.parent)
