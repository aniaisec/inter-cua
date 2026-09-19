"""Clickable paths and URLs: only on a terminal, never in piped output."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from cua.termlink import link


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_piped_output_is_plain_text() -> None:
    assert link("http://127.0.0.1:8100/", stream=io.StringIO()) == "http://127.0.0.1:8100/"
    assert link(Path("evidence/runs/run_1"), stream=io.StringIO()) == "evidence/runs/run_1"


def test_a_terminal_gets_a_hyperlink_showing_the_same_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CUA_NO_HYPERLINKS", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    url = link("http://127.0.0.1:8100/requests/req_1", stream=Tty())
    assert url == (
        "\x1b]8;;http://127.0.0.1:8100/requests/req_1\x1b\\"
        "http://127.0.0.1:8100/requests/req_1\x1b]8;;\x1b\\"
    )
    run = tmp_path / "run_1"
    path = link(run, stream=Tty())
    assert run.resolve().as_uri() in path and path.endswith(f"{run.as_posix()}\x1b]8;;\x1b\\")


def test_it_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUA_NO_HYPERLINKS", "1")
    assert link("http://x/", stream=Tty()) == "http://x/"
