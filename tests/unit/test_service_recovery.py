"""A stopped-writer snapshot preserves retry and consent authority after retention."""

from __future__ import annotations

import shutil

import pytest

from cua.maintenance import inspect
from cua.policy.tokens import SpentTokens, TokenRefused
from cua.replay import runner
from cua.replay.ownership import SQLiteInvocationStore
from tests.unit.test_invocation_ownership import GOAL2, invoke
from tests.unit.test_maintenance import completed
from tests.unit.test_replay import CONSENT


def test_quiescent_restore_preserves_answers_and_spent_consent(tmp_path, monkeypatch):
    source, restored = tmp_path / "source", tmp_path / "restored"
    # Include terminal evidence eligible for retention as well as a real replay
    # ownership reservation interrupted after a synthetic external commit.
    oracle = tmp_path / "external_commits.txt"

    def commit_then_interrupt(*args, **kwargs):
        oracle.write_text("commit\n", encoding="utf-8")
        SpentTokens(source).spend(CONSENT, kwargs["run_id"])
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "_replay", commit_then_interrupt)
    inputs = {"member_id": "10003", "initial_deposit": "10"}
    with pytest.raises(KeyboardInterrupt):
        invoke(source, path=GOAL2, inputs=inputs)
    completed(source, "archive")
    # All writers have exited their locks and database connections before copying.
    shutil.copytree(source, restored)
    monkeypatch.setattr(runner, "_replay", lambda *a, **k: pytest.fail("restore redispatched"))
    original = invoke(source, path=GOAL2, inputs=inputs)
    recovered = invoke(restored, path=GOAL2, inputs=inputs)
    assert recovered.model_dump(exclude={"evidence"}) == original.model_dump(exclude={"evidence"})
    assert recovered.evidence.run_dir == (restored / recovered.run_id).as_posix()
    assert recovered.code == "INTERRUPTED" and recovered.side_effect == "unknown"
    before = SQLiteInvocationStore.existing(restored).records()
    report = inspect(restored, "archive", apply=True)
    assert report["deleted"] == 1
    assert inspect(restored, "archive", apply=True)["deleted"] == 0
    assert SQLiteInvocationStore.existing(restored).records() == before
    assert invoke(restored, path=GOAL2, inputs=inputs) == recovered
    assert SpentTokens(restored).spent_by(CONSENT) == recovered.run_id
    with pytest.raises(TokenRefused, match="already consumed"):
        SpentTokens(restored).spend(CONSENT, "another-run")
    assert oracle.read_text(encoding="utf-8").splitlines() == ["commit"]
