"""Minimal sequential MCP client with bounded reads and owned-process cleanup."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from examples._support import ExampleError, process_options, require, stop_process


def configuration(root: Path) -> dict[str, Any]:
    # POSIX virtualenv interpreters are commonly symlinks. Resolving one to the
    # system interpreter would discard the environment with inter-cua installed.
    executable = Path(sys.executable).absolute()
    require(executable.is_file(), "the installed Python executable must exist")
    require((root / "cua.toml").is_file(), "select an initialized project root")
    return {
        "command": str(executable),
        "args": ["-m", "cua.cli", "mcp", "--root", str(root.resolve())],
    }


class StdioClient:
    def __init__(self, config: dict[str, Any], *, cwd: Path, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            [config["command"], *config["args"]],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            **process_options(),
        )
        self.messages: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader_error: Exception | None = None
        self.closed = False
        self.reader.start()
        self.next_id = 0

    def _read(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                self.messages.put(line)
        except (OSError, UnicodeError) as exc:
            self.reader_error = exc
        finally:
            self.messages.put(None)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.next_id += 1
        self.notify(method, params, request_id=self.next_id)
        try:
            line = self.messages.get(timeout=120)
        except queue.Empty:
            raise ExampleError("MCP response timed out; inspect the existing run") from None
        require(
            line is not None, f"MCP server ended before answering: {self.reader_error or 'EOF'}"
        )
        answer: dict[str, Any] = json.loads(line or "")
        require(answer.get("id") == self.next_id, "unexpected MCP response id")
        require("error" not in answer, f"MCP protocol error: {answer.get('error')}")
        return dict(answer["result"])

    def notify(
        self, method: str, params: dict[str, Any] | None = None, *, request_id: int | None = None
    ) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if request_id is not None:
            message["id"] = request_id
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        assert self.proc.stdin is not None
        try:
            try:
                self.proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass  # A server that already exited still needs to be reaped.
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        finally:
            try:
                stop_process(self.proc)
            finally:
                self.reader.join(timeout=10)
                assert self.proc.stdout is not None
                self.proc.stdout.close()
