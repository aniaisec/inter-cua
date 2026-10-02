"""Detached browser startup keeps CI sandbox opt-out explicit."""

import pytest

from cua.surface.playwright_surface import BrowserProcess
from cua.surface.protocol import Viewport


@pytest.mark.parametrize("setting,disabled", [(None, False), ("0", False), ("1", True)])
def test_chromium_sandbox_is_disabled_only_when_explicitly_requested(
    monkeypatch, tmp_path, setting, disabled
):
    monkeypatch.delenv("CUA_CHROMIUM_NO_SANDBOX", raising=False)
    if setting is not None:
        monkeypatch.setenv("CUA_CHROMIUM_NO_SANDBOX", setting)
    monkeypatch.setattr(
        "cua.surface.playwright_surface.tempfile.mkdtemp", lambda **kwargs: str(tmp_path)
    )
    commands = []

    def capture(args, **kwargs):
        commands.append(args)
        raise RuntimeError("captured startup")

    monkeypatch.setattr("cua.surface.playwright_surface.subprocess.Popen", capture)
    with pytest.raises(RuntimeError, match="captured startup"):
        BrowserProcess.start("chromium", port=9222, headless=True, viewport=Viewport(w=1280, h=800))
    assert ("--no-sandbox" in commands[0]) == disabled
    assert "--headless=new" in commands[0]
