"""Clickable paths and URLs in what the CLI prints for a person.

A run directory, a capability file or the operator console's address is the
next thing someone opens, so it is printed as an OSC 8 hyperlink: Windows
Terminal, iTerm2, GNOME Terminal and VS Code's terminal make it clickable,
and show the same text either way. Only when the stream is a terminal — piped
or redirected output (the JSON result on stdout, a captured log) stays plain
text, byte for byte. ``CUA_NO_HYPERLINKS=1`` turns it off.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TextIO

_OSC = "\x1b]8;;"
_ST = "\x1b\\"


def link(target: str | Path, text: str | None = None, *, stream: TextIO | None = None) -> str:
    """``target`` (a URL, or a path to open as a file URI) shown as ``text``."""
    shown = (
        text if text is not None else (target.as_posix() if isinstance(target, Path) else target)
    )
    if not _enabled(stream if stream is not None else sys.stderr):
        return shown
    if isinstance(target, Path) or not target.startswith(("http://", "https://", "file:")):
        uri = Path(target).resolve().as_uri()
    else:
        uri = target
    return f"{_OSC}{uri}{_ST}{shown}{_OSC}{_ST}"


def _enabled(stream: TextIO) -> bool:
    if os.environ.get("CUA_NO_HYPERLINKS", "") not in ("", "0"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False
