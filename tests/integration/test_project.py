"""A configured project discovers, reviews and replays from three locations."""

import json

import pytest
import yaml

from cua.artifact.store import load
from cua.cli import main
from tests.unit.test_project import REPO, project_files


@pytest.mark.browser
def test_project_discovery_review_and_replay_from_anywhere(
    tmp_path, mockapp_url, monkeypatch, capsys
):
    root = project_files(tmp_path / "project with spaces")
    tenant_file = root / "bindings/local.yaml"
    tenant = yaml.safe_load(tenant_file.read_text())
    tenant["base_url"] = mockapp_url
    tenant["secrets"] = {
        "mockcore/operator": {"provider": "env", "var": "P03_LOGIN", "format": "username:password"}
    }
    tenant_file.write_text(yaml.safe_dump(tenant))
    (root / ".env").write_text("P03_LOGIN=operator:operator\n")
    monkeypatch.chdir(tmp_path)
    assert (
        main(
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
            ]
        )
        == 0
    )
    capsys.readouterr()
    path = root / "tools/lookup.json"
    draft = load(path)
    assert draft.approval_state == "draft"
    assert list((root / "runtime/runs").glob("run_*/model_calls.jsonl"))

    nested = root / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert main(["describe", "lookup"]) == 0
    capsys.readouterr()
    assert (root / "runtime/state/reviews" / f"{draft.id}.json").is_file()
    assert main(["approve", "lookup", "--by", "P03 test"]) == 0
    capsys.readouterr()
    assert load(path).content_hash() == draft.content_hash()
    for cwd in (root, nested, tmp_path):
        monkeypatch.chdir(cwd)
        prefix = ["--root", str(root)] if cwd == tmp_path else []
        assert main([*prefix, "replay", "lookup", "--input", "member_id=10003"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["kind"] == "success"
        evidence = root / result["evidence"]["run_dir"]
        assert evidence.is_relative_to(root / "runtime/runs")
        assert not (evidence / "model_calls.jsonl").exists()
        assert load(path).content_hash() == draft.content_hash()
