"""Playwright traces for failed runs, with secrets scrubbed out.

A trace is the richest failure evidence there is — every action, a DOM snapshot
either side of it, the network — and for the same reason it is a leak: it
records the arguments of every call, so the password typed at sign-on is in it
verbatim, and so is the form post that carried it. Evidence is committed. So a
trace is kept only for a run that failed, and before it is written every text
entry in the archive has each secret replaced with ``***``. Binary entries
(screenshots) are left as they are: the browser draws a password field as dots.
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from playwright.sync_api import BrowserContext

from cua.policy.redaction import Redactor

TRACE_NAME = "trace.zip"


def start(context: BrowserContext) -> None:
    context.tracing.start(screenshots=True, snapshots=True)


def stop(context: BrowserContext, *, keep_as: Path | None, redactor: Redactor) -> Path | None:
    """Stop tracing; keep the trace (scrubbed) at ``keep_as``, or discard it."""
    if keep_as is None:
        context.tracing.stop()
        return None
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / TRACE_NAME
        context.tracing.stop(path=raw)
        scrub(raw, keep_as, redactor)
    return keep_as


def scrub(source: Path, target: Path, redactor: Redactor) -> None:
    with (
        zipfile.ZipFile(source) as zin,
        zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zout,
    ):
        for item in zin.infolist():
            data = zin.read(item.filename)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                zout.writestr(item, data)
                continue
            zout.writestr(item, redactor.text(text).encode("utf-8"))
