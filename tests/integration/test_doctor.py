"""The strict browser CI job must execute a real diagnostic launch probe."""

import json

import pytest
import yaml

from cua.cli import main
from cua.initialize import initialize

pytestmark = pytest.mark.browser


def test_doctor_real_browser_and_application_probes(tmp_path, mockapp_url, browser_session, capsys):
    # The shared fixture preserves --require-browser and recognized local skips.
    root = tmp_path / "doctor project with spaces"
    initialize(root, "demo")
    path = root / "tenants/local.yaml"
    tenant = yaml.safe_load(path.read_text(encoding="utf-8"))
    tenant["base_url"] = mockapp_url + "/login"
    path.write_text(yaml.safe_dump(tenant), encoding="utf-8")
    assert main(["--root", str(root), "doctor", "--json", "--probe-browser", "--probe-app"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    codes = {f["code"] for f in report["findings"]}
    # The mock login route handles GET, so the HEAD diagnostic reports 405.
    assert {"CHROMIUM_OK", "BROWSER_LAUNCH_OK", "APPLICATION_HEAD_UNSUPPORTED"} <= codes
    assert report["ready"] and captured.err == ""
    assert not (root / "evidence").exists()
    assert not (root / ".cua/reviews").exists()
