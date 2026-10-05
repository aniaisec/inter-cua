"""A configured project discovers, reviews and replays from three locations."""

import json
import subprocess
import sys

import pytest
import yaml

from cua.artifact.store import load
from tests.unit.test_project import REPO, project_files


@pytest.mark.browser
def test_project_discovery_review_and_replay_from_anywhere(tmp_path, mockapp_url, browser_session):
    # The shared browser fixture owns a sync Playwright loop on this thread.
    # Exercise the real CLI in separate processes, as an operator would.
    def cli(args, cwd):
        result = subprocess.run(
            [sys.executable, "-m", "cua.cli", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    root = project_files(tmp_path / "project with spaces")
    tenant_file = root / "bindings/local.yaml"
    tenant = yaml.safe_load(tenant_file.read_text())
    tenant["base_url"] = mockapp_url
    tenant["secrets"] = {
        "mockcore/operator": {"provider": "env", "var": "P03_LOGIN", "format": "username:password"}
    }
    tenant_file.write_text(yaml.safe_dump(tenant))
    (root / ".env").write_text("P03_LOGIN=operator:operator\n")
    cli(
        [
            "--root",
            str(root),
            "discover",
            "--goal",
            "Read savings balance",
            "--name",
            "lookup",
            "--entry",
            "/login",
            "--param",
            "member_id:string=10003",
            "--output",
            "savings_balance:decimal",
            "--output",
            "member_name:string?",
            "--llm",
            "scripted",
            "--script",
            str(REPO / "scripts/discovery/member_savings_balance.yaml"),
        ],
        tmp_path,
    )
    path = root / "tools/lookup.json"
    draft = load(path)
    assert draft.approval_state == "draft"
    assert list((root / "runtime/runs").glob("run_*/model_calls.jsonl"))

    nested = root / "nested"
    nested.mkdir()
    cli(["describe", "lookup"], nested)
    assert (root / "runtime/state/reviews" / f"{draft.id}.json").is_file()
    cli(["approve", "lookup", "--by", "P03 test"], nested)
    assert load(path).content_hash() == draft.content_hash()
    for cwd in (root, nested, tmp_path):
        prefix = ["--root", str(root)] if cwd == tmp_path else []
        result = json.loads(cli([*prefix, "replay", "lookup", "--input", "member_id=10003"], cwd))
        assert result["kind"] == "success"
        evidence = root / result["evidence"]["run_dir"]
        assert evidence.is_relative_to(root / "runtime/runs")
        assert not (evidence / "model_calls.jsonl").exists()
        assert load(path).content_hash() == draft.content_hash()
