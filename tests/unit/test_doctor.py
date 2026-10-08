"""Diagnostics must be deterministic, private, non-acting, and useful on failures."""

import errno
import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import yaml

from cua import doctor
from cua.artifact.store import load, save, with_changes
from cua.cli import main
from cua.initialize import initialize
from cua.policy.approval import approve, record_review
from cua.project import ProjectContext
from cua.surface.adapters import ADAPTERS, PLAYWRIGHT, WINDOWS_UIA

_INSTALLATIONS = doctor._chromium_installations


def codes(report):
    return {f.code for f in report.findings}


def inventory(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project with spaces"
    initialize(root, "demo")
    browser = tmp_path / "browser" / "chromium-123"
    browser.mkdir(parents=True)
    executable = browser / "chrome.exe"
    executable.write_bytes(b"synthetic executable")
    executable.chmod(0o755)
    monkeypatch.setattr(doctor, "_chromium_installations", lambda project: [browser])
    monkeypatch.setattr(doctor.metadata, "version", lambda name: "1.2.3")
    return ProjectContext.resolve(root, environ={})


def edit_yaml(project, relative, update):
    path = project.root / relative
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    update(data)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_static_checks_are_private_and_leave_project_unchanged(project):
    before = inventory(project.root)
    key = (project.root / ".cua/approval-signing.key").read_text().strip()
    with (
        patch.object(doctor, "_browser_probe", side_effect=AssertionError("browser probe")),
        patch.object(doctor, "_application_probe", side_effect=AssertionError("network probe")),
        patch("cua.agent.llm.select_client", side_effect=AssertionError("model call")),
        patch(
            "cua.surface.playwright_surface.PlaywrightSurface.launch",
            side_effect=AssertionError("UI"),
        ),
    ):
        report = doctor.diagnose(project, "local")
    assert report.ready
    assert {
        "PROJECT_OK",
        "TENANT_OK",
        "POLICY_OK",
        "FAMILY_OK",
        "SECRET_OK",
        "CHROMIUM_OK",
        "CAPABILITY_OK",
    } <= codes(report)
    assert "CAPABILITY_NOT_APPROVED" in codes(report)
    data = json.loads(report.to_json())
    assert data["report_version"] == 1 and data["ready"]
    assert data["project"]["adapter"]["name"] == "playwright"
    assert data["project"]["config_version"] == 1
    assert key not in report.to_json() + report.text()
    assert "operator:operator" not in report.to_json() + report.text()
    assert "configuration v1" in report.text()
    assert "capability schema 1.3" in report.text()
    assert all(f.code and f.explanation and f.remedy for f in report.findings)
    assert inventory(project.root) == before
    assert not (project.root / "evidence").exists()


@pytest.mark.parametrize("location", ["nested", "unrelated"])
def test_cli_uses_project_paths_and_outputs_clean_json(
    project, tmp_path, monkeypatch, capsys, location
):
    invocation = project.root / "nested" if location == "nested" else tmp_path / "unrelated"
    invocation.mkdir()
    monkeypatch.chdir(invocation)
    arguments = [] if location == "nested" else ["--root", str(project.root)]
    assert main([*arguments, "doctor", "--tenant", "local", "--json"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["project"]["root"] == str(project.root)
    assert captured.err == ""
    assert Path.cwd() == invocation


def test_doctor_text_and_failed_check_exit(project, capsys):
    (project.root / "policies/default.yaml").unlink()
    assert main(["--root", str(project.root), "doctor"]) == 1
    captured = capsys.readouterr()
    assert "[error] POLICY_INVALID" in captured.out
    assert "Remedy:" in captured.out and "checks failed" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    "config", ["malformed = [", "version = 99", "version = 1\nunknown = 'private-canary'"]
)
def test_bad_project_configuration_is_usage_error_with_private_json(tmp_path, capsys, config):
    (tmp_path / "cua.toml").write_text(config)
    assert main(["--root", str(tmp_path), "doctor", "--json"]) == 64
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert not report["ready"] and report["findings"][0]["code"] == "PROJECT_INVALID"
    assert "private-canary" not in captured.out
    assert captured.err == ""


def test_bad_root_is_json_usage_error(tmp_path, capsys):
    assert main(["--root", str(tmp_path / "missing"), "doctor", "--json"]) == 64
    assert json.loads(capsys.readouterr().out)["findings"][0]["code"] == "PROJECT_INVALID"


@pytest.mark.parametrize(
    "arguments", [["--typo", "private-canary"], ["--tenant"], ["--probe-browser=bad"]]
)
def test_malformed_doctor_arguments_use_exit_64_and_json(capsys, arguments):
    assert main(["doctor", "--json", *arguments]) == 64
    captured = capsys.readouterr()
    assert json.loads(captured.out)["findings"][0]["code"] == "INVOCATION_INVALID"
    assert "private-canary" not in captured.out and captured.err == ""


def test_other_commands_keep_argparse_errors_and_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["surfaces", "--typo"])
    assert exc.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        main(["doctor", "--help"])
    assert exc.value.code == 0
    assert "--probe-app" in capsys.readouterr().out
    assert not doctor.is_invocation(["--root", "doctor", "surfaces"])
    assert doctor.is_invocation(["--root=somewhere", "doctor"])


@pytest.mark.parametrize(
    "tenant_data",
    [None, "secrets: [private-canary", "id: private-canary", "base_url: ['private-canary']"],
)
def test_missing_or_bad_tenant_is_a_failed_check(project, tenant_data):
    path = project.root / "tenants/local.yaml"
    if tenant_data is None:
        path.unlink()
    else:
        path.write_text(tenant_data)
    report = doctor.diagnose(project, "local")
    assert not report.ready and "TENANT_INVALID" in codes(report)
    assert "private-canary" not in report.to_json()


@pytest.mark.parametrize(
    "relative,code",
    [
        ("policies/default.yaml", "POLICY_INVALID"),
        ("capabilities/families/legacy-core.yaml", "FAMILY_INVALID"),
    ],
)
@pytest.mark.parametrize("contents", [None, "private-canary: [", "private-canary: value"])
def test_invalid_policy_and_family_are_private_failed_checks(project, relative, code, contents):
    path = project.root / relative
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents)
    report = doctor.diagnose(project, "local")
    assert not report.ready and code in codes(report)
    assert "private-canary" not in report.to_json()


@pytest.mark.parametrize(
    "field", ["allowed_paths", "credential_paths", "sensitive_labels", "download_pattern"]
)
def test_invalid_policy_regex_is_diagnosed_before_network(project, field):
    value = ["["] if field.endswith("paths") else "["
    edit_yaml(project, "policies/default.yaml", lambda data: data.update({field: value}))
    with patch.object(doctor, "_application_probe", side_effect=AssertionError("network")):
        report = doctor.diagnose(project, "local", probe_app=True)
    assert {"POLICY_INVALID", "APPLICATION_PROBE_BLOCKED"} <= codes(report)


@pytest.mark.parametrize(
    "url,code",
    [
        ("ftp://app.local", "APPLICATION_LOCATION_INVALID"),
        ("https://name:private-canary@app.local", "APPLICATION_LOCATION_INVALID"),
        ("http://[", "APPLICATION_LOCATION_INVALID"),
        ("http://app.local:bad", "APPLICATION_LOCATION_INVALID"),
        ("http://999.999.999.999", "APPLICATION_LOCATION_INVALID"),
        ("http://app.local/\x01private-canary", "APPLICATION_LOCATION_INVALID"),
        ("http://", "APPLICATION_LOCATION_INVALID"),
        ("https://replace-me.invalid", "APPLICATION_LOCATION_PLACEHOLDER"),
    ],
)
def test_invalid_locations_are_never_probed_or_echoed(project, url, code):
    edit_yaml(project, "tenants/local.yaml", lambda data: data.update(base_url=url))
    with patch.object(doctor, "_application_probe", side_effect=AssertionError("network")):
        report = doctor.diagnose(project, "local", probe_app=True)
    assert code in codes(report) and not report.ready
    assert "private-canary" not in report.to_json()


def test_policy_blocked_origin_skips_probe(project):
    edit_yaml(
        project,
        "policies/default.yaml",
        lambda data: data.update(allowed_origins=["https://elsewhere.local"]),
    )
    with patch.object(doctor, "_application_probe", side_effect=AssertionError("network")):
        report = doctor.diagnose(project, "local", probe_app=True)
    assert {"APPLICATION_POLICY_BLOCKED", "APPLICATION_PROBE_BLOCKED"} <= codes(report)


def test_base_path_need_not_be_entry_path_but_probe_respects_paths(project):
    edit_yaml(
        project, "policies/default.yaml", lambda data: data.update(allowed_paths=["^/login$"])
    )
    assert doctor.diagnose(project, "local").ready
    with patch.object(doctor, "_application_probe", side_effect=AssertionError("network")):
        report = doctor.diagnose(project, "local", probe_app=True)
    assert "APPLICATION_PROBE_BLOCKED" in codes(report)


def test_missing_secret_does_not_create_signing_key(project):
    key_path = project.root / ".cua/approval-signing.key"
    key_path.unlink()
    project.environ["CUA_SECRET_MOCKCORE_OPERATOR"] = ""
    report = doctor.diagnose(project, "local")
    missing = [f for f in report.findings if f.code == "SECRET_UNAVAILABLE"]
    assert len(missing) == 2 and not report.ready
    assert not key_path.exists()


def test_bad_secret_shape_and_file_encoding_do_not_echo_values(project):
    project.environ["CUA_SECRET_MOCKCORE_OPERATOR"] = "private-canary"
    (project.root / ".cua/approval-signing.key").write_bytes(b"\xff")
    report = doctor.diagnose(project, "local")
    assert len([f for f in report.findings if f.code == "SECRET_UNAVAILABLE"]) == 2
    assert "private-canary" not in report.to_json()


def test_empty_capability_catalog_is_warning_and_corrupt_artifact_fails(project):
    for path in project.path("capabilities").glob("*.json"):
        path.unlink()
    report = doctor.diagnose(project, "local")
    assert report.ready and "CAPABILITY_NONE" in codes(report)
    (project.path("capabilities") / "broken.json").write_text('{"secret": "private-canary"}')
    report = doctor.diagnose(project, "local")
    assert not report.ready and "CAPABILITY_INVALID" in codes(report)
    assert "private-canary" not in report.to_json()


def test_invalid_registry_is_failed_check(project):
    directory = project.path("capabilities") / "registry"
    directory.mkdir()
    (directory / "lifecycle.jsonl").write_text("private-canary")
    report = doctor.diagnose(project, "local")
    assert "CAPABILITY_DIRECTORY_INVALID" in codes(report)
    assert "private-canary" not in report.to_json()


def test_incompatible_capability_fails_and_unrelated_family_is_ignored(project, monkeypatch):
    monkeypatch.setitem(ADAPTERS, "web", PLAYWRIGHT.model_copy(update={"features": frozenset()}))
    report = doctor.diagnose(project, "local")
    assert "CAPABILITY_INCOMPATIBLE" in codes(report)
    edit_yaml(project, "tenants/local.yaml", lambda data: data.update(capabilities=[]))
    assert "CAPABILITY_INCOMPATIBLE" not in codes(doctor.diagnose(project, "local"))


def test_capability_surface_must_match_tenant_target(project):
    path = project.path("capabilities") / "member_savings_balance.json"
    cap = load(path)
    save(with_changes(cap, target=cap.target.model_copy(update={"surface": "desktop"})), path)
    report = doctor.diagnose(project, "local")
    assert "CAPABILITY_INCOMPATIBLE" in codes(report)


@pytest.mark.parametrize(
    "ref,fields",
    [
        ("secret://another/mockcore/operator", ["username", "password"]),
        ("secret://{tenant.id}/missing", ["username", "password"]),
        ("secret://{tenant.id}/cua/approval-signing-key", ["username", "password"]),
        ("secret://{tenant.id}/mockcore/operator", ["username", "password", "extra"]),
    ],
)
def test_capability_credential_binding_must_be_usable_by_this_tenant(project, ref, fields):
    path = project.path("capabilities") / "member_savings_balance.json"
    data = json.loads(path.read_text())
    credential = next(iter(data["credentials"].values()))
    credential.update(ref=ref, fields=fields)
    path.write_text(json.dumps(data))
    report = doctor.diagnose(project, "local")
    assert "CAPABILITY_CREDENTIAL_INVALID" in codes(report)
    assert not report.ready


def test_recorded_approval_is_verified_without_creating_receipts(project):
    path = project.path("capabilities") / "member_savings_balance.json"
    cap = load(path)
    record_review(cap, path, state_dir=project.path("state"))
    approve(path, "reviewer", state_dir=project.path("state"))
    before = inventory(project.root)
    assert "CAPABILITY_APPROVAL_MISSING" not in codes(doctor.diagnose(project, "local"))
    assert inventory(project.root) == before
    (project.path("capabilities") / "registry/lifecycle.jsonl").unlink()
    report = doctor.diagnose(project, "local")
    assert "CAPABILITY_APPROVAL_MISSING" in codes(report)


def test_capabilities_path_that_is_file_fails(project):
    path = project.root / "capabilities-file"
    path.write_text("keep")
    report = doctor.diagnose(project, "local", capabilities_dir=path)
    assert "CAPABILITY_DIRECTORY_INVALID" in codes(report)
    assert path.read_text() == "keep"


@pytest.mark.parametrize("name", ["headless_shell.exe", "chrome-headless-shell.exe"])
def test_chromium_headless_shell_layouts_are_recognized(project, tmp_path, name):
    path = tmp_path / "chromium_headless_shell-123"
    path.mkdir()
    executable = path / name
    executable.write_text("synthetic")
    executable.chmod(0o755)
    with patch.object(doctor, "_chromium_installations", return_value=[path]):
        assert "CHROMIUM_OK" in codes(doctor.diagnose(project, "local"))


def test_configured_runtime_directories_are_checked_and_removed(project):
    (project.root / "cua.toml").write_text(
        'version = 1\n[paths]\nruns = "runtime/deep/runs"\nstate = "runtime/deep/state"\n'
    )
    selected = ProjectContext.resolve(project.root, environ={})
    report = doctor.diagnose(selected, "local")
    assert report.ready
    assert len([f for f in report.findings if f.code == "RUNTIME_WRITABLE"]) == 2
    assert not (project.root / "runtime").exists()


def test_write_denied_cleans_new_directories_and_reports_failure(project):
    with patch.object(
        doctor.tempfile, "NamedTemporaryFile", side_effect=PermissionError("private-canary")
    ):
        report = doctor.diagnose(project, "local")
    assert "RUNTIME_NOT_WRITABLE" in codes(report) and not report.ready
    assert not (project.root / "evidence").exists()
    assert "private-canary" not in report.to_json()


def test_runtime_path_is_file_is_failed_check(project):
    (project.root / "evidence").write_text("keep")
    report = doctor.diagnose(project, "local")
    assert "RUNTIME_NOT_WRITABLE" in codes(report)
    assert (project.root / "evidence").read_text() == "keep"


def test_write_probe_preserves_concurrent_files(tmp_path):
    original = doctor.tempfile.NamedTemporaryFile

    def create_concurrent(*args, **kwargs):
        (Path(kwargs["dir"]) / "another-writer").write_text("keep")
        return original(*args, **kwargs)

    with patch.object(doctor.tempfile, "NamedTemporaryFile", side_effect=create_concurrent):
        doctor._write_probe(tmp_path / "new")
    assert (tmp_path / "new/another-writer").read_text() == "keep"
    assert len(list((tmp_path / "new").iterdir())) == 1


def test_chromium_absent_and_permission_denied_are_distinct(project, tmp_path):
    with patch.object(doctor, "_chromium_installations", return_value=[tmp_path / "absent"]):
        assert "CHROMIUM_MISSING" in codes(doctor.diagnose(project, "local"))
    with patch.object(doctor.os, "access", return_value=False):
        assert "CHROMIUM_NOT_EXECUTABLE" in codes(doctor.diagnose(project, "local"))


@pytest.mark.parametrize(
    "error",
    [
        PermissionError("private-canary"),
        ValueError("private-canary"),
        subprocess.TimeoutExpired("private-canary", 15),
    ],
)
def test_chromium_inspection_failure_is_private(project, error):
    with patch.object(doctor, "_chromium_installations", side_effect=error):
        report = doctor.diagnose(project, "local")
    assert "CHROMIUM_CHECK_FAILED" in codes(report)
    assert "private-canary" not in report.to_json()


def test_playwright_dry_run_uses_snapshot_and_filters_installations(project):
    output = (
        "Install location: /cache/chromium-123\n"
        "Install location: /cache/ffmpeg-123\n"
        "Install location: /cache/chromium_headless_shell-123\n"
    )
    with patch.object(
        doctor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output)
    ) as run:
        paths = _INSTALLATIONS(project)
    assert [p.name for p in paths] == ["chromium-123", "chromium_headless_shell-123"]
    assert run.call_args.kwargs["env"] == project.environ
    assert run.call_args.kwargs["cwd"] == project.root
    assert run.call_args.args[0][-3:] == ["install", "--dry-run", "chromium"]


def test_unrecognized_playwright_dry_run_output_is_a_failure(project):
    with patch.object(
        doctor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "private-canary")
    ):
        with pytest.raises(ValueError, match="no Chromium"):
            _INSTALLATIONS(project)


def test_cleanup_failure_is_not_reported_as_writable(tmp_path):
    with patch.object(Path, "rmdir", side_effect=OSError(errno.EACCES, "denied")):
        with pytest.raises(OSError):
            doctor._write_probe(tmp_path / "new")


@pytest.mark.parametrize(
    "package,required,code",
    [
        ("playwright", True, "DEPENDENCY_MISSING"),
        ("comtypes", True, "UIA_DEPENDENCY_MISSING"),
        ("anthropic", False, "DEPENDENCY_MISSING"),
    ],
)
def test_missing_dependencies_report_install_remedy(project, package, required, code):
    with patch.object(doctor.metadata, "version", side_effect=doctor.metadata.PackageNotFoundError):
        report = doctor.Report()
        assert not doctor._dependency(report, package, required=required)
    assert code in codes(report)
    assert report.ready is (not required)
    assert "pip install" in report.findings[0].remedy


def test_missing_playwright_skips_browser_probe(project):
    def version(name):
        if name in ("playwright", "inter-cua"):
            raise doctor.metadata.PackageNotFoundError
        return "1.2.3"

    with (
        patch.object(doctor.metadata, "version", side_effect=version),
        patch.object(doctor, "_browser_probe", side_effect=AssertionError("browser")),
    ):
        report = doctor.diagnose(project, "local", probe_browser=True)
    assert "BROWSER_PROBE_SKIPPED" in codes(report)
    assert report.project["package_version"] == "uninstalled"


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_desktop_platform_dependencies_and_unsupported_probes(project, monkeypatch, platform):
    desktop = project.root / "desktop"
    initialize(desktop, "windows")
    selected = ProjectContext.resolve(desktop, environ={})
    monkeypatch.setattr(doctor.sys, "platform", platform)
    if platform == "win32":
        monkeypatch.setitem(ADAPTERS, "desktop", WINDOWS_UIA)
    else:
        monkeypatch.delitem(ADAPTERS, "desktop", raising=False)
    with patch.object(doctor, "_chromium", side_effect=AssertionError("browser")):
        report = doctor.diagnose(selected, "local", probe_browser=True, probe_app=True)
    assert {
        "DESKTOP_SESSION_UNVERIFIED",
        "BROWSER_PROBE_UNSUPPORTED",
        "APPLICATION_PROBE_UNSUPPORTED",
    } <= codes(report)
    if platform == "linux":
        assert "ADAPTER_UNAVAILABLE" in codes(report)
    else:
        assert report.project["adapter"]["name"] == "windows-uia"


def test_family_placeholder_and_surface_mismatch(project):
    edit_yaml(
        project,
        "capabilities/families/legacy-core.yaml",
        lambda data: data["target"].update(surface="desktop", vendor="REPLACE_ME"),
    )
    report = doctor.diagnose(project, "local")
    assert {"FAMILY_PLACEHOLDER", "FAMILY_SURFACE_MISMATCH"} <= codes(report)


@pytest.mark.parametrize(
    "result,code",
    [
        ("BROWSER_LAUNCH_OK", "BROWSER_LAUNCH_OK"),
        ("BROWSER_EXECUTABLE_MISSING", "BROWSER_EXECUTABLE_MISSING"),
        ("BROWSER_LAUNCH_DENIED", "BROWSER_LAUNCH_DENIED"),
        ("private-canary", "BROWSER_LAUNCH_FAILED"),
        ([], "BROWSER_LAUNCH_FAILED"),
    ],
)
def test_browser_probe_child_results_are_sanitized(project, result, code):
    with patch.object(
        doctor.subprocess,
        "run",
        return_value=subprocess.CompletedProcess([], 0, json.dumps({"code": result})),
    ) as run:
        report = doctor.diagnose(project, "local", probe_browser=True)
    assert code in codes(report)
    assert "private-canary" not in report.to_json()
    assert run.call_args.kwargs["env"] == project.environ
    assert run.call_args.kwargs["cwd"] == project.root


@pytest.mark.parametrize(
    "error,code",
    [
        (PermissionError("private-canary"), "BROWSER_LAUNCH_DENIED"),
        (subprocess.TimeoutExpired("private-canary", 15), "BROWSER_LAUNCH_TIMEOUT"),
        (
            subprocess.CalledProcessError(1, "private-canary", stderr="secret-value"),
            "BROWSER_LAUNCH_FAILED",
        ),
    ],
)
def test_browser_process_failures_are_private(project, error, code):
    with patch.object(doctor.subprocess, "run", side_effect=error):
        report = doctor.diagnose(project, "local", probe_browser=True)
    assert code in codes(report)
    assert "private-canary" not in report.to_json() and "secret-value" not in report.to_json()


@pytest.mark.parametrize(
    "error,code",
    [
        (None, "BROWSER_LAUNCH_OK"),
        (RuntimeError("Executable doesn't exist private-canary"), "BROWSER_EXECUTABLE_MISSING"),
        (RuntimeError("Access is denied private-canary"), "BROWSER_LAUNCH_DENIED"),
        (RuntimeError("Timeout 5000ms exceeded private-canary"), "BROWSER_LAUNCH_TIMEOUT"),
        (RuntimeError("Unexpected private-canary"), "BROWSER_LAUNCH_FAILED"),
    ],
)
def test_browser_child_classifies_launch_and_closes_driver(error, code):
    manager = MagicMock()
    launch = manager.__enter__.return_value.chromium.launch
    if error:
        launch.side_effect = error
    stdout = io.StringIO()
    with (
        patch("playwright.sync_api.sync_playwright", return_value=manager),
        redirect_stdout(stdout),
    ):
        exec(doctor._BROWSER_SCRIPT, {})
    assert json.loads(stdout.getvalue())["code"] == code
    assert "private-canary" not in stdout.getvalue()
    manager.__exit__.assert_called_once()
    if error is None:
        launch.return_value.close.assert_called_once()


@pytest.mark.parametrize(
    "status,code",
    [
        (200, "APPLICATION_REACHABLE"),
        (302, "APPLICATION_REACHABLE"),
        (401, "APPLICATION_AUTH_REQUIRED"),
        (403, "APPLICATION_AUTH_REQUIRED"),
        (405, "APPLICATION_HEAD_UNSUPPORTED"),
        (501, "APPLICATION_HEAD_UNSUPPORTED"),
        (503, "APPLICATION_HTTP_ERROR"),
    ],
)
def test_application_probe_head_only_no_credentials_or_redirects(project, status, code):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"location": "https://outside.local"}, content=b"private-canary"
        )

    original = httpx.Client

    def client(**kwargs):
        assert kwargs == {"timeout": 5.0, "follow_redirects": False, "trust_env": False}
        return original(transport=httpx.MockTransport(respond), **kwargs)

    with patch.object(doctor.httpx, "Client", side_effect=client):
        report = doctor.diagnose(project, "local", probe_app=True)
    assert code in codes(report)
    assert len(requests) == 1 and requests[0].method == "HEAD"
    assert "authorization" not in requests[0].headers and "cookie" not in requests[0].headers
    assert "private-canary" not in report.to_json()


@pytest.mark.parametrize(
    "error,code",
    [
        (httpx.ConnectError("private-canary"), "APPLICATION_UNREACHABLE"),
        (httpx.ReadTimeout("private-canary"), "APPLICATION_TIMEOUT"),
    ],
)
def test_application_failures_do_not_echo_response_or_url(project, error, code):
    with patch.object(doctor.httpx.Client, "head", side_effect=error):
        report = doctor.diagnose(project, "local", probe_app=True)
    assert code in codes(report) and not report.ready
    assert "private-canary" not in report.to_json()
