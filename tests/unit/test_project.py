"""Project paths must not depend on the caller or a service worker's cwd."""

import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from cua.api.access import Gate, load_access
from cua.api.models import RunRequest
from cua.api.service import RunService, ServiceSettings
from cua.artifact.store import load
from cua.cli import _build_parser, main
from cua.project import ProjectContext, bind_cli
from cua.replay.result import Success
from cua.secrets.resolver import resolve
from cua.tenant import load_tenant

REPO = Path(__file__).resolve().parents[2]


def project_files(root):
    root.mkdir()
    (root / "cua.toml").write_text(
        'version = 1\ndefault_tenant = "local"\n[paths]\n'
        'capabilities = "tools"\ntenants = "bindings"\n'
        'families = "templates"\nruns = "runtime/runs"\nstate = "runtime/state"\n'
    )
    shutil.copytree(REPO / "capabilities", root / "tools")
    shutil.copytree(REPO / "policies", root / "policies")
    shutil.copytree(REPO / "capabilities/families", root / "templates")
    (root / "bindings").mkdir()
    (root / "bindings/local.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "local",
                "app_family": "legacy-core",
                "base_url": "http://127.0.0.1:8000",
                "secrets": {"login": {"provider": "file", "path": "login.txt"}},
            }
        )
    )
    (root / "login.txt").write_text("synthetic-login")
    (root / "api").mkdir()
    (root / "api/access.yaml").write_text(
        yaml.safe_dump(
            {
                "tenants": {
                    "local": {"file": "bindings/local.yaml", "policy": "policies/default.yaml"}
                },
                "clients": {
                    "agent": {
                        "key": {"provider": "file", "path": "api-key.txt"},
                        "tenants": ["local"],
                        "capabilities": ["*"],
                        "scopes": ["read", "invoke"],
                    }
                },
            }
        )
    )
    (root / "api-key.txt").write_text("synthetic-key-" + "a" * 32)
    return root


def parsed(project, argv):
    parser = _build_parser()
    args = parser.parse_args(argv)
    args.project = project
    bind_cli(parser, args, argv)
    return args


def test_selection_nearest_ancestor_explicit_root_and_legacy(tmp_path):
    outer = project_files(tmp_path / "project with spaces")
    inner = project_files(outer / "nested project")
    nested = inner / "a/b"
    nested.mkdir(parents=True)
    assert ProjectContext.resolve(cwd=nested).root == inner
    assert ProjectContext.resolve(outer, cwd=nested).root == outer
    assert ProjectContext.resolve(cwd=tmp_path).root == tmp_path
    assert not ProjectContext.resolve(cwd=tmp_path).configured


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "version = 2",
        "version = true",
        'version = "1"',
        "version = 1\nunknown = 2",
        "version = [",
        'version = 1\n[paths]\nruns = ""',
    ],
)
def test_malformed_config_never_falls_back(tmp_path, contents):
    root = project_files(tmp_path / "project")
    (root / "cua.toml").write_text(contents)
    nested = root / "nested"
    nested.mkdir()
    with pytest.raises(ValueError):
        ProjectContext.resolve(cwd=nested)
    with pytest.raises(ValueError):
        ProjectContext.resolve(root, cwd=tmp_path)


def test_missing_root_is_usage_error_and_does_not_change_cwd(tmp_path, capsys):
    before = Path.cwd()
    assert main(["--root", str(tmp_path / "missing"), "catalog"]) == 64
    assert "not a directory" in capsys.readouterr().err
    assert Path.cwd() == before


def test_paths_defaults_explicit_equal_default_and_absolute_override(tmp_path):
    root = project_files(tmp_path / "project")
    project = ProjectContext.resolve(root, cwd=tmp_path, environ={})
    args = parsed(project, ["discover", "--goal", "lookup"])
    assert args.runs_dir == root / "runtime/runs"
    assert args.capabilities_dir == root / "tools"
    assert args.families_dir == root / "templates"
    args = parsed(
        project,
        [
            "discover",
            "--goal",
            "lookup",
            "--runs-dir=evidence/runs",
            "--families-dir",
            str(tmp_path / "external"),
            "--policy",
            "p.yaml",
        ],
    )
    assert args.runs_dir == tmp_path / "evidence/runs"
    assert args.families_dir == tmp_path / "external"
    assert args.policy == tmp_path / "p.yaml"
    assert parsed(project, ["workflow", "check", "named"]).workflow == "named"
    assert parsed(project, ["workflow", "check", "custom.yaml"]).workflow == str(
        tmp_path / "custom.yaml"
    )


def test_configured_absolute_paths_and_default_tenant(tmp_path):
    root = project_files(tmp_path / "project")
    external = tmp_path / "external tools"
    (root / "cua.toml").write_text(
        'version = 1\ndefault_tenant = "other"\n[paths]\n'
        f"capabilities = {json.dumps(str(external))}\n"
    )
    project = ProjectContext.resolve(root, cwd=tmp_path, environ={})
    assert project.path("capabilities") == external
    assert parsed(project, ["discover", "--goal", "lookup"]).tenant == "other"
    assert parsed(project, ["discover", "--goal", "lookup", "--tenant", "local"]).tenant == "local"
    assert parsed(project, ["schema"]).out.parent == external / "schema"


def test_tenant_file_references_and_desktop_child_cwd_are_project_relative(tmp_path):
    root = project_files(tmp_path / "project")
    project = ProjectContext.resolve(root, cwd=tmp_path, environ={})
    tenant = load_tenant("local", project=project)
    assert tenant.policy_file == root / "policies/default.yaml"
    assert tenant.secrets["login"].path == str(root / "login.txt")
    assert resolve("secret://local/login", tenant).field("value") == "synthetic-login"
    (root / "bindings/desk.yaml").write_text((REPO / "tenants/desk.yaml").read_text())
    assert load_tenant("desk", project=project).desktop.cwd == str(root)


def test_environment_snapshot_isolated_with_process_precedence(tmp_path, monkeypatch):
    a, b = project_files(tmp_path / "a"), project_files(tmp_path / "b")
    (a / ".env").write_text("P03_ONLY_A='a'\nP03_SHARED=from-a\nEMPTY=\n")
    (b / ".env").write_text("P03_ONLY_B=b\nP03_SHARED=from-b\n")
    (tmp_path / ".env").write_text("P03_WRONG_ROOT=yes\n")
    monkeypatch.setenv("P03_SHARED", "process")
    before = dict(os.environ)
    pa = ProjectContext.resolve(a, cwd=tmp_path)
    pb = ProjectContext.resolve(b, cwd=tmp_path)
    assert pa.environ["P03_SHARED"] == pb.environ["P03_SHARED"] == "process"
    assert pa.environ["P03_ONLY_A"] == "a" and "P03_ONLY_A" not in pb.environ
    assert "P03_ONLY_B" not in pa.environ and "P03_WRONG_ROOT" not in pa.environ
    assert "EMPTY" not in pa.environ
    assert dict(os.environ) == before


@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_selected_provider_receives_project_snapshot_without_environment_mutation(
    tmp_path, monkeypatch, provider
):
    from cua.agent.llm import select_client

    root = project_files(tmp_path / "project")
    variable = "ANTHROPIC_API_KEY" if provider == "anthropic" else "GEMINI_API_KEY"
    (root / ".env").write_text(f"{variable}=synthetic-project-key\n")
    project = ProjectContext.resolve(root, environ={})
    monkeypatch.setenv(variable, "synthetic-other-key")
    captured = []
    if provider == "anthropic":
        import anthropic

        monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: captured.append(kw) or object())
    else:
        from google import genai

        monkeypatch.setattr(genai, "Client", lambda **kw: captured.append(kw) or object())
    assert select_client(provider, environ=project.environ).provider == provider
    assert captured[0]["api_key"] == "synthetic-project-key"
    assert os.environ[variable] == "synthetic-other-key"


def test_anthropic_snapshot_cannot_inherit_a_later_auth_token(tmp_path, monkeypatch):
    import anthropic

    from cua.agent.llm import select_client

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-other-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://synthetic-other.invalid")
    captured = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: captured.append(kw) or object())
    select_client("anthropic", environ={"ANTHROPIC_API_KEY": "synthetic-project-key"})
    assert captured[0]["auth_token"] == ""
    assert captured[0]["base_url"] == "https://api.anthropic.com"


def test_bound_benchmark_paths_preserve_definition_hashes(tmp_path):
    from cua.benchmark.registry import load_suite
    from cua.benchmark.subject import task_version

    projects = [project_files(tmp_path / name) for name in ("a", "b")]
    for root in projects:
        shutil.copytree(REPO / "bench/tasks", root / "bench/tasks")
    original = load_suite("core", root=REPO / "bench/tasks").tasks[0]
    for root in projects:
        project = ProjectContext.resolve(root, cwd=tmp_path, environ={})
        bound = load_suite("core", root=root / "bench/tasks", project=project).tasks[0]
        assert Path(bound.capability).is_absolute()
        assert task_version(bound) == task_version(original)
        changed = bound.model_copy(update={"inputs": {"member_id": "10004"}})
        assert task_version(changed) != task_version(bound)


def test_benchmark_provenance_uses_project_and_selected_pricing(tmp_path, monkeypatch):
    import hashlib
    from contextlib import nullcontext

    from cua.benchmark import runner
    from cua.benchmark.environment import app_version

    root = project_files(tmp_path / "project")
    (root / "mockapp").mkdir()
    (root / "mockapp/app.py").write_text("# synthetic application\n")
    pricing = root / "custom-pricing.yaml"
    pricing.write_text("# selected pricing\n")
    project = ProjectContext.resolve(root, cwd=tmp_path, environ={})
    git_calls = []

    def git(*args, cwd=None):
        git_calls.append(cwd)
        return "synthetic-revision" if args[0] == "rev-parse" else ""

    monkeypatch.setattr(runner, "_git", git)
    monkeypatch.setattr(runner, "_browser_version", lambda: "synthetic-browser")
    monkeypatch.setattr(runner, "mockapp", lambda **kwargs: nullcontext("http://127.0.0.1:8000"))
    monkeypatch.chdir(tmp_path)
    result = runner.run_session(
        runner.Plan(
            tasks=[],
            strategies=[],
            suite="synthetic",
            project=project,
            pricing_path=pricing,
            runs_root=root / "bench/runs",
            reports_dir=root / "bench/reports",
        )
    )
    assert result.git_commit == "synthetic-revision" and not result.git_dirty
    assert git_calls == [root, root]
    assert result.app_version == app_version(root / "mockapp")
    assert result.pricing_sha256 == hashlib.sha256(pricing.read_bytes()).hexdigest()


def test_demo_preparation_uses_configured_family_templates(tmp_path):
    from cua.demo.runner import prepare

    root = project_files(tmp_path / "project")
    shutil.copytree(REPO / "scripts/discovery", root / "scripts/discovery")
    shutil.copytree(REPO / "bench/security", root / "bench/security")
    template = root / "templates/legacy-core.yaml"
    text = template.read_text() + "\n# configured family marker\n"
    template.write_text(text)
    project = ProjectContext.resolve(root, environ={})
    work, _ = prepare(tmp_path / "demo", "http://127.0.0.1:8000", project=project)
    assert (work / "capabilities/families/legacy-core.yaml").read_text() == text


def test_api_version_details_read_review_receipts_from_project_state(tmp_path):
    from cua.api.routes.capabilities import _versions
    from cua.artifact.store import save, with_changes
    from cua.policy.approval import record_review

    project = ProjectContext.resolve(project_files(tmp_path / "project"), environ={})
    path = project.path("capabilities") / "member_savings_balance.json"
    draft = save(
        with_changes(
            load(path), version=4, approval_state="draft", approved_by=None, approved_at=None
        ),
        path,
    )
    record_review(draft, path, state_dir=project.path("state"))
    config = load_access(project.path("access"), project=project)
    gate = Gate(config, project=project)
    caller = gate.caller("Bearer " + "synthetic-key-" + "a" * 32, "local", "req")
    service = RunService(ServiceSettings(project.path("runs"), project=project))
    try:
        versions = _versions(caller, service, draft.name)
        assert next(v for v in versions if v.record.version == 4).status == "review"
    finally:
        service.close()


@pytest.mark.parametrize("location", ["root", "nested", "unrelated"])
def test_catalog_and_seals_independent_of_invocation_directory(
    tmp_path, monkeypatch, capsys, location
):
    root = project_files(tmp_path / "project with spaces")
    nested = root / "nested"
    nested.mkdir()
    monkeypatch.chdir({"root": root, "nested": nested, "unrelated": tmp_path}[location])
    seal = load(root / "tools/member_savings_balance.json").content_hash()
    argv = ["catalog", "--json"]
    if location == "unrelated":
        argv = ["--root", str(root), *argv]
    assert main(argv) == 0
    tools = json.loads(capsys.readouterr().out)
    assert any(t["name"] == "member_savings_balance" for t in tools)
    assert load(root / "tools/member_savings_balance.json").content_hash() == seal
    assert parsed(
        ProjectContext.resolve(root), ["replay", "member_savings_balance"]
    ).capability.is_absolute()


def test_mcp_root_placement_and_conflicts(tmp_path, monkeypatch):
    import cua.mcp.cli

    root = project_files(tmp_path / "project with spaces")
    monkeypatch.chdir(tmp_path)
    captured = []
    monkeypatch.setattr(cua.mcp.cli, "main", lambda args: captured.append(args) or 0)
    assert main(["mcp", "--root", str(root)]) == 0
    assert main(["--root", str(root), "mcp"]) == 0
    assert captured[0].access == captured[1].access == root / "api/access.yaml"
    assert Path.cwd() == tmp_path
    assert main(["--root", str(root), "mcp", "--root", str(tmp_path)]) == 64
    assert len(captured) == 2


def test_missing_family_refuses_before_model_or_ui_and_no_record_bypasses(
    tmp_path, monkeypatch, capsys
):
    import cua.agent.llm

    root = project_files(tmp_path / "project")
    (root / "templates/legacy-core.yaml").unlink()
    monkeypatch.setattr(
        cua.agent.llm, "select_client", lambda *a, **kw: pytest.fail("model started")
    )
    argv = ["--root", str(root), "discover", "--goal", "lookup", "--llm", "scripted"]
    assert main(argv) == 64
    assert "no template" in capsys.readouterr().err
    assert not (root / "runtime").exists()
    assert main([*argv, "--no-record"]) == 64
    assert "needs --script" in capsys.readouterr().err


def test_malformed_family_fails_preflight_with_usage_error(tmp_path, monkeypatch, capsys):
    import cua.agent.llm

    root = project_files(tmp_path / "project")
    (root / "templates/legacy-core.yaml").write_text("target: [")
    monkeypatch.setattr(
        cua.agent.llm, "select_client", lambda *a, **kw: pytest.fail("model started")
    )
    assert main(["--root", str(root), "discover", "--goal", "lookup"]) == 64
    assert "invalid family template" in capsys.readouterr().err
    assert not (root / "runtime").exists()


def test_two_threaded_services_keep_paths_and_secret_bindings(tmp_path, monkeypatch):
    projects = [
        ProjectContext.resolve(project_files(tmp_path / name), environ={}) for name in ("a", "b")
    ]
    entered = threading.Barrier(3)
    release = threading.Event()
    calls = []

    def replay(path, *, tenant, runs_dir, environ, **kwargs):
        entered.wait(timeout=10)
        assert release.wait(timeout=10)
        calls.append(
            (
                path,
                runs_dir,
                tenant.policy_file,
                resolve("secret://local/login", tenant).field("value"),
                environ,
            )
        )
        return Success(
            capability="member_savings_balance",
            capability_version=3,
            outputs={"savings_balance": "1411.21"},
        )

    services = []
    try:
        for project in projects:
            config = load_access(project.path("access"), project=project)
            gate = Gate(config, project=project, environ=project.environ)
            caller = gate.caller("Bearer " + "synthetic-key-" + "a" * 32, "local", "req")
            service = RunService(
                ServiceSettings(project.path("runs"), project=project), replay=replay
            )
            services.append((service, caller))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    service.submit,
                    caller,
                    RunRequest(capability="member_savings_balance", inputs={"member_id": "10003"}),
                    None,
                )
                for service, caller in services
            ]
            records = [future.result()[0] for future in futures]
        entered.wait(timeout=10)
        monkeypatch.chdir(tmp_path)
        release.set()
    finally:
        release.set()
        for service, _ in services:
            service.close()
    assert len(calls) == 2
    for project in projects:
        call = next(c for c in calls if c[1] == project.path("runs"))
        assert call[0].is_relative_to(project.path("capabilities"))
        assert call[2] == project.relative("policies/default.yaml")
        assert call[3] == "synthetic-login"
    assert all(
        service.get(caller, record.run_id).state == "finished"
        for (service, caller), record in zip(services, records, strict=True)
    )
