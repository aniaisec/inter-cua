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


def test_browser_launch_uses_the_invocation_environment(monkeypatch, tmp_path):
    from cua.surface.playwright_surface import _headed_from_env

    monkeypatch.setenv("CUA_CHROMIUM_NO_SANDBOX", "1")
    monkeypatch.setenv("CUA_HEADED", "1")
    environment = {"CUA_CHROMIUM_NO_SANDBOX": "0", "CUA_HEADED": "0"}
    monkeypatch.setattr(
        "cua.surface.playwright_surface.tempfile.mkdtemp", lambda **kwargs: str(tmp_path)
    )
    captured = []

    def capture(args, **kwargs):
        captured.append((args, kwargs))
        raise RuntimeError("captured startup")

    monkeypatch.setattr("cua.surface.playwright_surface.subprocess.Popen", capture)
    with pytest.raises(RuntimeError, match="captured startup"):
        BrowserProcess.start(
            "chromium",
            port=9222,
            headless=True,
            viewport=Viewport(w=1280, h=800),
            environ=environment,
        )
    assert "--no-sandbox" not in captured[0][0]
    assert captured[0][1]["env"] == environment
    assert not _headed_from_env(environment)
