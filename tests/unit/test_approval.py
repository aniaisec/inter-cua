"""Saving, versioning, describing and approving a capability.

The rule under test: an approval covers exactly the content a person was
shown. Any change afterwards — through ``save`` or by hand — is a new version,
in draft, that has to be described again before it can be approved.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cua.artifact.describe import describe
from cua.artifact.schema import Capability
from cua.artifact.store import ArtifactError, load, open_capability, save, with_changes
from cua.cli import main
from cua.policy.approval import ApprovalRefused, approve, last_review, record_review
from tests.unit import artifacts


@pytest.fixture
def cap_file(tmp_path: Path) -> Path:
    path = tmp_path / "capabilities" / "member_savings_balance.json"
    save(artifacts.record_goal1(), path)
    return path


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def described(path: Path, state: Path) -> Capability:
    cap = load(path)
    record_review(cap, path, state_dir=state)
    return cap


def hand_edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# -- store ----------------------------------------------------------------------


def test_save_seals_and_load_round_trips(cap_file: Path) -> None:
    cap = load(cap_file)
    assert cap.content_sha256 == cap.content_hash()
    assert (cap.version, cap.approval_state) == (1, "draft")
    assert not open_capability(cap_file).edited_outside


def test_saving_different_content_is_a_new_draft_version_of_the_same_capability(
    cap_file: Path, state: Path
) -> None:
    described(cap_file, state)
    approved = approve(cap_file, "anil", state_dir=state)
    assert (approved.version, approved.approval_state) == (1, "approved")

    changed = with_changes(approved, id="cap_01M2TD0000000000000000000Z", description="Changed.")
    saved = save(changed, cap_file)
    assert (saved.version, saved.approval_state, saved.approved_by) == (2, "draft", None)
    assert saved.id == approved.id  # a new version, not a second capability


def test_saving_the_same_content_keeps_version_and_approval(cap_file: Path, state: Path) -> None:
    described(cap_file, state)
    approved = approve(cap_file, "anil", state_dir=state)
    again = save(approved, cap_file)
    assert (again.version, again.approval_state) == (1, "approved")


def test_a_hand_edit_is_loaded_as_the_next_version_in_draft(cap_file: Path, state: Path) -> None:
    described(cap_file, state)
    approve(cap_file, "anil", state_dir=state)
    hand_edit(cap_file, '"name": "Search"', '"name": "Find"')

    loaded = open_capability(cap_file)
    assert loaded.edited_outside
    assert loaded.sealed_version == 1
    assert (loaded.capability.version, loaded.capability.approval_state) == (2, "draft")
    assert loaded.capability.approved_by is None


def test_an_invalid_file_is_refused_with_its_reasons(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ArtifactError, match="is not JSON"):
        load(bad)
    with pytest.raises(ArtifactError, match="no such file"):
        load(tmp_path / "missing.json")


# -- approval --------------------------------------------------------------------


def test_approve_flips_draft_to_approved_after_a_describe(cap_file: Path, state: Path) -> None:
    described(cap_file, state)
    cap = approve(cap_file, "anil", state_dir=state)

    assert (cap.approval_state, cap.approved_by) == ("approved", "anil")
    assert cap.approved_at is not None
    on_disk = json.loads(cap_file.read_text(encoding="utf-8"))
    assert (on_disk["approval_state"], on_disk["approved_by"]) == ("approved", "anil")
    assert on_disk["content_sha256"] == cap.content_hash()


def test_approve_refuses_what_was_never_described(cap_file: Path, state: Path) -> None:
    with pytest.raises(ApprovalRefused, match="has not been described yet"):
        approve(cap_file, "anil", state_dir=state)
    assert load(cap_file).approval_state == "draft"


def test_approve_refuses_a_capability_edited_since_its_describe(
    cap_file: Path, state: Path
) -> None:
    described(cap_file, state)
    hand_edit(cap_file, '"name": "Search"', '"name": "Find"')

    with pytest.raises(ApprovalRefused, match=r"v2 has changed since it was last described"):
        approve(cap_file, "anil", state_dir=state)

    # Reading it again is what makes it approvable.
    described(cap_file, state)
    cap = approve(cap_file, "anil", state_dir=state)
    assert (cap.version, cap.approval_state) == (2, "approved")
    assert not open_capability(cap_file).edited_outside


def test_approve_refuses_after_a_re_record_until_described_again(
    cap_file: Path, state: Path
) -> None:
    described(cap_file, state)
    approve(cap_file, "anil", state_dir=state)
    rerecorded = with_changes(artifacts.record_goal1(), description="Re-recorded.")
    assert save(rerecorded, cap_file).version == 2

    with pytest.raises(ApprovalRefused, match="described as v1"):
        approve(cap_file, "anil", state_dir=state)


def test_approve_needs_a_name(cap_file: Path, state: Path) -> None:
    described(cap_file, state)
    with pytest.raises(ApprovalRefused, match="say who is approving"):
        approve(cap_file, "  ", state_dir=state)


def test_the_review_receipt_records_what_was_shown(cap_file: Path, state: Path) -> None:
    cap = described(cap_file, state)
    review = last_review(cap, state_dir=state)
    assert review is not None
    assert (review.version, review.content_sha256) == (1, cap.content_hash())


# -- describe --------------------------------------------------------------------


def test_describe_reads_as_a_plain_account_of_the_capability() -> None:
    text = describe(artifacts.record_goal1())

    for expected in [
        "Capability    member_savings_balance  (version 1)",
        "Status        DRAFT - not approved; it will not run unattended",
        "Side effects  none - it only reads. Safe to run again with the same inputs.",
        "member_id           text, required, must match ^[0-9]+$",
        "app_login           username, password from secret://{tenant.id}/mockcore/operator",
        'read from the "Balance" column of the row with "Savings" in the main pane, as US dollars',
        'Type the caller\'s member_id into the textbox next to "Member ID" in the main pane.',
        'Type the app_login password (secret) into the textbox next to "Password".',
        'found by: the button named "Search" in the main pane  [role_name]',
        "CHECKPOINT cp.member_detail:",
        "the caller's member_id is shown in the main pane",
        'NOT_FOUND           business outcome - after search.submit, if "No matching member"',
        "SESSION_EXPIRED     once past cp.logged_in",
        "by scripted (scripted)",
    ]:
        assert expected in text, expected
    assert text.isascii()
    assert "10003" not in text


def test_describe_warns_about_an_irreversible_step() -> None:
    doc = json.loads(artifacts.record_goal1().to_json())
    step = doc["steps"][4]
    step.update(risk="irreversible", approval="required", retry={"allowed": False})
    doc["contract"].update(side_effects="creates_record", idempotent=False, may_escalate=True)
    text = describe(Capability.model_validate(doc))

    assert "!! COMMITS A CHANGE. Needs approval; never retried automatically." in text
    assert "CREATES A RECORD" in text
    assert "NOT safe to simply run again" in text
    assert "Human needed  yes - search.submit must be approved" in text


# -- the CLI ----------------------------------------------------------------------


def test_cli_describe_then_approve_then_refuse_a_hand_edit(
    cap_file: Path, state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path, st = str(cap_file), str(state)

    assert main(["approve", path, "--by", "anil", "--state-dir", st]) == 1
    assert "has not been described yet" in capsys.readouterr().err

    assert main(["describe", path, "--state-dir", st]) == 0
    assert "To approve exactly this: cua approve" in capsys.readouterr().out

    assert main(["approve", path, "--by", "anil", "--state-dir", st]) == 0
    assert "version 1 is approved by anil" in capsys.readouterr().out

    hand_edit(cap_file, '"name": "Search"', '"name": "Find"')
    assert main(["approve", path, "--by", "anil", "--state-dir", st]) == 1
    assert "refused" in capsys.readouterr().err

    assert main(["describe", path, "--state-dir", st]) == 0
    out = capsys.readouterr().out
    assert "edited by hand after it was saved as version 1" in out
    assert "(version 2)" in out and "DRAFT" in out


def test_cli_describe_reports_an_invalid_capability(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = json.loads(artifacts.GOLDEN.read_text(encoding="utf-8"))
    doc["steps"][4]["action"] = "hover"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(doc), encoding="utf-8")

    assert main(["describe", str(bad), "--state-dir", str(tmp_path / "s")]) == 1
    err = capsys.readouterr().err
    assert "is not a valid capability" in err
    assert "steps.4.action" in err


def test_cli_record_writes_a_draft_and_versions_a_re_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "cap.json"
    args = [
        "record",
        str(artifacts.GOAL1_RUN),
        "--out",
        str(out),
        "--policy",
        str(artifacts.REPO / "policies" / "default.yaml"),
        "--families-dir",
        str(artifacts.FAMILIES),
    ]
    assert main(args) == 0
    assert "version 1 (draft)" in capsys.readouterr().err
    first = load(out)

    # The same run records to the same content: not a new version.
    assert main(args) == 0
    assert load(out) == first

    # A different run of the same capability is.
    rerun = tmp_path / "rerun"
    shutil.copytree(artifacts.GOAL1_RUN, rerun)
    with (rerun / "log.jsonl").open("a") as log:
        log.write("\n")
    assert main([*args[:1], str(rerun), *args[2:]]) == 0
    second = load(out)
    assert (second.version, second.id) == (2, first.id)


def test_cli_record_refuses_a_run_that_did_not_finish(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    shutil.copytree(artifacts.GOAL1_RUN, run_dir)
    result = json.loads((run_dir / "result.json").read_text())
    result.update(kind="interrupted", reason="KeyboardInterrupt")
    (run_dir / "result.json").write_text(json.dumps(result))

    args = ["record", str(run_dir), "--out", str(tmp_path / "c.json")]
    args += ["--policy", str(artifacts.REPO / "policies" / "default.yaml")]
    args += ["--families-dir", str(artifacts.FAMILIES)]
    assert main(args) == 1
    assert "ended interrupted (KeyboardInterrupt)" in capsys.readouterr().err
    assert not (tmp_path / "c.json").exists()
