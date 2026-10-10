"""Failure-path cleanup of the example clients, without application interaction."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from examples._support import ExampleError, process_options, stop_process
from examples.mcp_setup.client import StdioClient


def test_stdio_client_reaps_an_exited_server_even_if_stdin_close_breaks(tmp_path):
    client = StdioClient(
        {"command": sys.executable, "args": ["-c", "pass"]},
        cwd=tmp_path,
        env=dict(os.environ),
    )
    stdin = client.proc.stdin
    try:
        client.proc.wait(timeout=10)
        client.proc.stdin = Mock()
        client.proc.stdin.close.side_effect = BrokenPipeError("server already exited")
        client.close()
        client.close()  # Cleanup can safely run again from an outer finally.
        assert not client.reader.is_alive() and client.proc.poll() is not None
        assert client.proc.stdout.closed
    finally:
        if stdin:
            stdin.close()
        if client.proc.poll() is None:
            stop_process(client.proc)


def test_stdio_reader_reports_invalid_utf8_without_waiting_for_response_timeout(tmp_path):
    client = StdioClient(
        {
            "command": sys.executable,
            "args": ["-c", "import sys; sys.stdout.buffer.write(b'\\xff\\n')"],
        },
        cwd=tmp_path,
        env=dict(os.environ),
    )
    try:
        client.proc.wait(timeout=10)
        client.reader.join(timeout=10)
        assert isinstance(client.reader_error, UnicodeError)
        assert client.messages.get_nowait() is None
    finally:
        client.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill fallback")
def test_failed_tree_cleanup_still_kills_and_reaps_parent(monkeypatch):
    proc = Mock()
    proc.poll.return_value = None
    monkeypatch.setattr(
        "examples._support.subprocess.run", lambda *args, **kwargs: Mock(returncode=1)
    )
    with pytest.raises(ExampleError, match="parent was reaped"):
        stop_process(proc)
    proc.kill.assert_called_once()
    proc.wait.assert_called_once_with(timeout=10)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_cleanup_stops_a_descendant_after_its_parent_has_exited(tmp_path: Path):
    # The descendant inherits stdout. Cleanup must release that pipe even after
    # the session leader has exited; terminating only the parent leaks the reader.
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])",
        ],
        stdout=subprocess.PIPE,
        **process_options(),
    ) as proc:
        try:
            proc.wait(timeout=10)
            stop_process(proc)
            assert proc.stdout.read() == b""
        finally:
            stop_process(proc)
