"""Installed gates release Windows directory locks without suppressing failures."""

import os
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

from cua.demo import runner
from scripts.ci import check_installed


def sharing_error():
    error = PermissionError("workspace is still in use")
    error.winerror = 32
    return error


def test_temporary_workspace_retries_a_released_windows_lock(tmp_path, monkeypatch):
    workspace = tempfile.TemporaryDirectory(dir=tmp_path)
    cleanup = workspace.cleanup
    calls = []

    def locked_cleanup():
        calls.append(True)
        if len(calls) <= 2:
            raise sharing_error()
        cleanup()

    monkeypatch.setattr(workspace, "cleanup", locked_cleanup)
    monkeypatch.setattr(check_installed.tempfile, "TemporaryDirectory", lambda **kwargs: workspace)
    monkeypatch.setattr(check_installed, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        check_installed, "time", SimpleNamespace(monotonic=time.monotonic, sleep=lambda delay: None)
    )
    with check_installed.temporary_workspace() as root:
        (root / "result.json").write_text("{}")
    assert len(calls) == 3 and not root.exists()


def test_temporary_workspace_does_not_hide_a_persistent_lock(tmp_path, monkeypatch):
    workspace = tempfile.TemporaryDirectory(dir=tmp_path)
    cleanup = workspace.cleanup
    clock = iter([0, 6])

    def locked_cleanup():
        raise sharing_error()

    monkeypatch.setattr(workspace, "cleanup", locked_cleanup)
    monkeypatch.setattr(check_installed.tempfile, "TemporaryDirectory", lambda **kwargs: workspace)
    monkeypatch.setattr(check_installed, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(check_installed, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    try:
        with (
            pytest.raises(PermissionError, match="still in use"),
            check_installed.temporary_workspace(),
        ):
            pass
    finally:
        cleanup()


def test_temporary_workspace_preserves_a_test_failure():
    with pytest.raises(AssertionError, match="failed stock check"):
        with check_installed.temporary_workspace() as root:
            assert root.exists()
            raise AssertionError("failed stock check")
    assert not root.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows current-directory locking")
def test_temporary_workspace_waits_for_a_real_directory_lock():
    process = None
    release = None
    try:
        with check_installed.temporary_workspace() as root:
            process = subprocess.Popen(
                [
                    sys._base_executable,
                    "-c",
                    "import time; from pathlib import Path; "
                    "Path('ready').write_text('ready'); time.sleep(120)",
                ],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 10
            while not (root / "ready").exists():
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.05)
            (root / "ready").unlink()
            with pytest.raises(PermissionError) as locked:
                root.rmdir()
            assert locked.value.winerror == 32
            release = threading.Timer(0.3, process.terminate)
            release.start()
        assert not root.exists()
        assert process.wait(timeout=10) is not None
    finally:
        if release is not None:
            release.cancel()
            release.join(timeout=10)
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-tree shutdown")
def test_server_stops_a_child_holding_the_workspace(tmp_path, monkeypatch):
    workspace = tempfile.TemporaryDirectory(dir=tmp_path)
    monkeypatch.setattr(check_installed.tempfile, "TemporaryDirectory", lambda **kwargs: workspace)
    monkeypatch.setattr(
        runner, "sys", SimpleNamespace(platform="win32", executable=sys._base_executable)
    )
    monkeypatch.setattr(
        runner.httpx, "get", lambda *args, **kwargs: SimpleNamespace(status_code=200)
    )
    code = (
        "import subprocess, sys, time; from pathlib import Path; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
        "Path('child.tmp').write_text(str(child.pid)); "
        "Path('child.tmp').replace('child.pid'); time.sleep(120)"
    )
    child_pid = None
    try:
        with check_installed.temporary_workspace() as root:
            (root / "parent_server.py").write_text(code)
            with runner.server(["parent_server"], "http://unused", root, dict(os.environ)):
                deadline = time.monotonic() + 10
                while not (root / "child.pid").exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                child_pid = int((root / "child.pid").read_text())
        assert not root.exists()
    finally:
        if child_pid is not None and root.exists():
            subprocess.run(
                ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
