"""Starter projects must be editable, valid, private, and safe to repeat."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from cua.artifact.recorder import load_family
from cua.artifact.store import load
from cua.cli import main
from cua.initialize import TEMPLATES, initialize, template_files
from cua.policy.allowlist import load_policy
from cua.project import ProjectContext
from cua.tenant import load_tenant


@pytest.mark.parametrize("template", TEMPLATES)
def test_all_templates_parse_and_begin_without_approval(tmp_path, template):
    root = tmp_path / "project with spaces"
    initialize(root, template)
    project = ProjectContext.resolve(root, environ={})
    tenant = load_tenant("local", project=project)
    policy = load_policy(Path(tenant.policy), tenant)
    family = load_family(tenant.app_family, project.path("families"))
    assert family.target.surface == ("desktop" if template == "windows" else "web")
    assert policy.blocked_actions == ["download", "external_navigation"]
    assert (root / ".gitignore").is_file()
    assert "cua " in (root / "README.md").read_text()
    assert not (root / ".cua/reviews").exists()
    for path in (root / "capabilities").glob("*.json"):
        capability = load(path)
        assert capability.approval_state == "draft"
        assert capability.approved_by is None
    if template != "demo":
        assert not (root / "api/access.yaml").exists()
        assert not (root / ".env").exists()


def test_dry_run_creates_nothing_and_generates_no_key(tmp_path):
    root = tmp_path / "project"
    with patch("cua.initialize.secrets.token_hex", side_effect=AssertionError("generated a key")):
        names = initialize(root, "demo", dry_run=True)
    assert ".cua/approval-signing.key" in names
    assert ".env" in names and "cua.toml" in names
    assert not root.exists()


def test_parent_segments_do_not_create_unused_directories(tmp_path):
    initialize(tmp_path / "unused" / ".." / "project", "web")
    assert (tmp_path / "project/cua.toml").is_file()
    assert not (tmp_path / "unused").exists()


def test_repeated_init_preserves_edits_and_secrets(tmp_path):
    initialize(tmp_path, "demo")
    (tmp_path / "tenants/local.yaml").write_text("my edited tenant")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(FileExistsError):
        initialize(tmp_path, "demo")
    assert before == {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize(
    "conflict", ["tenants", "policies/default.yaml", ".cua/approval-signing.key"]
)
def test_all_conflicts_preflight_before_writing(tmp_path, conflict):
    path = tmp_path / conflict
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("keep")
    with pytest.raises(FileExistsError):
        initialize(tmp_path, "demo")
    assert path.read_text() == "keep"
    assert not (tmp_path / "cua.toml").exists()


def test_projects_have_different_private_keys(tmp_path):
    for name in ("a", "b"):
        initialize(tmp_path / name, "demo")
    key = Path(".cua/approval-signing.key")
    assert (tmp_path / "a" / key).read_bytes() != (tmp_path / "b" / key).read_bytes()
    if os.name != "nt":
        assert (tmp_path / "a" / key).stat().st_mode & 0o777 == 0o600


def test_write_failure_rolls_back_only_our_files(tmp_path):
    (tmp_path / "unrelated.txt").write_text("keep")
    original = os.open
    calls = 0

    def fail(path, flags, mode=0o777):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise PermissionError("simulated disk failure")
        return original(path, flags, mode)

    with patch("cua.initialize.os.open", side_effect=fail), pytest.raises(PermissionError):
        initialize(tmp_path, "web")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["unrelated.txt"]


def test_symlink_destination_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks requires local Windows privileges")
    with pytest.raises(ValueError, match="symlink"):
        initialize(link, "demo")
    assert not list(real.iterdir())


def test_init_works_next_to_bad_config_and_lists_no_secret(tmp_path, monkeypatch, capsys):
    (tmp_path / "cua.toml").write_text("broken config")
    monkeypatch.chdir(tmp_path)
    assert main(["init", "child", "--template", "web"]) == 0
    output = capsys.readouterr().out
    key = (tmp_path / "child/.cua/approval-signing.key").read_text().strip()
    assert key not in output
    assert main(["init", "child", "--template", "web"]) == 64


def test_resource_inventory_has_no_receipts_or_keys():
    files = template_files("demo")
    assert "scripts/discovery/member_savings_balance.yaml" in files
    assert "bench/security/scripts/external_link.yaml" in files
    assert "capabilities/schema/capability-1.3.json" in files
    assert not any(name.startswith(".cua/") for name in files)
    for name, data in files.items():
        if name.startswith("capabilities/") and name.count("/") == 1:
            assert json.loads(data)["approval_state"] == "draft"


def test_edited_invalid_family_fails_before_model_or_ui(tmp_path, monkeypatch, capsys):
    initialize(tmp_path, "web")
    monkeypatch.setenv("CUA_APP_LOGIN", "synthetic:synthetic")
    (tmp_path / "capabilities/families/my-web-app.yaml").write_text("target: [invalid]")
    with (
        patch("cua.agent.llm.select_client", side_effect=AssertionError("model started")),
        patch(
            "cua.surface.playwright_surface.PlaywrightSurface.launch",
            side_effect=AssertionError("UI started"),
        ),
    ):
        assert main(["--root", str(tmp_path), "discover", "--goal", "Lookup"]) == 64
    assert "invalid" in capsys.readouterr().err.lower()
