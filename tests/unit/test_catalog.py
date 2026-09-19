"""``cua catalog``: the approved capabilities as tools, and typed invocation.

Nothing here starts a browser: every request below is turned away, or
listed, before one would be.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from cua import catalog
from cua.artifact.store import load
from cua.cli import main
from tests.conftest import REPO_ROOT

CAPS = REPO_ROOT / "capabilities"
GOAL1 = CAPS / "member_savings_balance.json"
GOAL2 = CAPS / "open_subaccount.json"


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def with_draft(tmp_path: Path) -> Path:
    """The two committed capabilities, plus one edited by hand: a draft."""
    for src in (GOAL1, GOAL2):
        (tmp_path / src.name).write_bytes(src.read_bytes())
    edited = json.loads(GOAL1.read_text(encoding="utf-8"))
    edited["name"] = "member_savings_balance_copy"
    (tmp_path / "copy.json").write_text(json.dumps(edited), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    return tmp_path


# -- listing ---------------------------------------------------------------------------


def test_the_catalog_offers_only_what_unattended_replay_would_run(with_draft: Path) -> None:
    entries, broken = catalog.scan(with_draft)
    assert [e.capability.name for e in entries] == [
        "member_savings_balance",
        "member_savings_balance_copy",
        "open_subaccount",
    ]
    assert [b.path.name for b in broken] == ["broken.json"]
    draft = entries[1]
    assert not draft.invocable and draft.edited_outside
    assert [t["name"] for t in catalog.tools(entries)] == [
        "member_savings_balance",
        "open_subaccount",
    ]
    assert len(catalog.tools(entries, include_drafts=True)) == 3


def test_a_tool_definition_carries_the_typed_inputs_and_the_contract() -> None:
    tool = catalog.tool_definition(load(GOAL2))
    assert set(tool) == {"name", "description", "input_schema"}
    schema = tool["input_schema"]
    assert schema["required"] == ["member_id", "initial_deposit"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["member_id"]["type"] == "string"
    assert schema["properties"]["member_id"]["pattern"] == "^[0-9]+$"
    assert schema["properties"]["initial_deposit"]["type"] == ["string", "number"]
    text = tool["description"]
    for promise in ("reference_number", "idempotency_key", "NEEDS_APPROVAL", "VALIDATION_ERROR"):
        assert promise in text
    assert "Read only" in catalog.tool_description(load(GOAL1))


def test_cua_catalog_lists_and_prints_tool_definitions(
    with_draft: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(["catalog", "--capabilities-dir", str(with_draft)], capsys)
    assert code == 0
    assert "member_savings_balance  v3  approved" in out
    assert "_copy" not in out and "1 draft(s) not shown" in out
    assert "broken.json" in err

    code, out, _ = run(["catalog", "--json", "--capabilities-dir", str(with_draft)], capsys)
    assert code == 0
    assert [t["name"] for t in json.loads(out)] == ["member_savings_balance", "open_subaccount"]


# -- typed arguments -------------------------------------------------------------------


def test_arguments_are_checked_against_their_declared_types() -> None:
    cap = load(GOAL2)
    inputs, problems = catalog.coerce(
        cap, {"member_id": "00042", "initial_deposit": Decimal("250.00")}
    )
    assert inputs == {"member_id": "00042", "initial_deposit": "250.00"} and not problems

    assert catalog.coerce(cap, {"initial_deposit": 250.5})[0] == {"initial_deposit": "250.5"}
    assert catalog.coerce(cap, {"initial_deposit": Decimal("1E+3")})[0] == {
        "initial_deposit": "1000"
    }

    _, problems = catalog.coerce(cap, {"member_id": 42, "initial_deposit": True})
    assert problems == [
        "member_id=42 must be a JSON string (quoted), not a number",
        "initial_deposit=true is not a decimal",
    ]
    # Not an input: passed on for replay's own check to name.
    assert catalog.coerce(cap, {"branch": 7}) == ({"branch": "7"}, [])


def test_a_request_is_an_object_with_known_fields_and_exact_numbers() -> None:
    req = catalog.parse_request('{"capability": "x", "inputs": {"initial_deposit": 0.10}}')
    assert req["inputs"]["initial_deposit"] == Decimal("0.10")
    with pytest.raises(ValueError, match="unknown request field"):
        catalog.parse_request('{"capability": "x", "input": {}}')
    with pytest.raises(ValueError, match="JSON object"):
        catalog.parse_request("[]")
    with pytest.raises(ValueError, match="not JSON"):
        catalog.parse_arguments("{member_id: 1}")


def test_a_wrongly_typed_call_fails_input_invalid_with_no_browser(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = run(
        ["catalog", "invoke", "member_savings_balance", "--args", '{"member_id": 10003}'],
        capsys,
    )
    result = json.loads(out)
    assert code == 1
    assert result["kind"] == "failure" and result["code"] == "INPUT_INVALID"
    assert result["side_effect"] == "none" and result["run_id"] is None


def test_a_request_that_breaks_the_input_pattern_is_refused_by_replay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"capability": "member_savings_balance", "inputs": {"member_id": "abc"}}),
        encoding="utf-8",
    )
    code, out, _ = run(
        ["catalog", "invoke", "--request", str(request), "--runs-dir", str(tmp_path / "runs")],
        capsys,
    )
    result = json.loads(out)
    assert code == 1 and result["code"] == "INPUT_INVALID"
    assert "does not match" in result["message"]
    assert not (tmp_path / "runs").exists() or not any((tmp_path / "runs").glob("run_*"))


def test_unknown_names_and_mismatched_requests_are_usage_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run(["catalog", "invoke", "close_account"], capsys)
    assert code == 64 and "known: member_savings_balance, open_subaccount" in err

    request = tmp_path / "request.json"
    request.write_text('{"capability": "open_subaccount"}', encoding="utf-8")
    code, _, err = run(
        ["catalog", "invoke", "member_savings_balance", "--request", str(request)], capsys
    )
    assert code == 64 and "the request is for 'open_subaccount'" in err

    code, _, err = run(["catalog", "invoke"], capsys)
    assert code == 64 and "name the capability" in err
