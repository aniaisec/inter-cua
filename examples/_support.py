"""Small helpers shared by the examples, never an alternate replay engine."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Any


class ExampleError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ExampleError(message)


def check_subset(actual: Any, expected: Any, path: str = "result") -> None:
    """Expected JSON objects are subsets; arrays and scalar types are exact."""
    if isinstance(expected, dict):
        require(isinstance(actual, dict), f"{path}: expected an object")
        for key, value in expected.items():
            require(key in actual, f"{path}.{key}: missing")
            check_subset(actual[key], value, f"{path}.{key}")
    elif isinstance(expected, list):
        require(isinstance(actual, list) and len(actual) == len(expected), f"{path}: array length")
        for index, value in enumerate(expected):
            check_subset(actual[index], value, f"{path}[{index}]")
    else:
        require(
            type(actual) is type(expected) and actual == expected,
            f"{path}: {actual!r} != {expected!r}",
        )


def process_options() -> dict[str, Any]:
    """A private POSIX process group allows cleanup of child processes as well."""
    return {"start_new_session": True} if sys.platform != "win32" else {}


def stop_process(proc: subprocess.Popen[Any]) -> None:
    """Reap a process started with process_options, including its children."""
    cleanup_failed = False
    if sys.platform != "win32":
        # The parent may already have exited while a descendant still holds a pipe.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        if sys.platform == "win32":
            try:
                stopped = subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                cleanup_failed = stopped.returncode != 0 and proc.poll() is None
            except (OSError, subprocess.TimeoutExpired):
                cleanup_failed = True
            if cleanup_failed and proc.poll() is None:
                proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    if sys.platform != "win32":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    require(not cleanup_failed, "process tree cleanup failed; the parent was reaped")
