from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cua.cli import main
from cua.maintenance import inspect, safe_path
from cua.persistence import atomic_write_json
from cua.replay.ownership import OwnedInvocation, SQLiteInvocationStore, execution_lock, identity
from cua.replay.result import Failure, Success


def completed(root: Path, tenant: str = "local", *, unknown: bool = False) -> Path:
    store = SQLiteInvocationStore(root)
    run = root / f"run_{tenant}"
    run.mkdir()
    atomic_write_json(
        run / "run.json",
        {
            "kind": "replay",
            "run_id": run.name,
            "tenant": {"id": tenant},
            "capability": {"name": "lookup", "version": 1},
        },
    )
    (run / "screenshots").mkdir()
    (run / "screenshots/0001.png").write_bytes(b"image")
    (run / "log.jsonl").write_text("", encoding="utf-8")
    common = dict(capability="lookup", capability_version=1, run_id=run.name)
    result = (
        Failure(code="INTERRUPTED", message="interrupted", side_effect="unknown", **common)
        if unknown
        else Success(outputs={}, **common)
    )
    atomic_write_json(run / "result.json", result.model_dump(mode="json"))
    os.utime(run / "result.json", (1, 1))
    key = identity(tenant, "key")
    store.save(
        key,
        OwnedInvocation(
            tenant=tenant,
            key_hash=key,
            fingerprint="fp",
            capability="lookup",
            version=1,
            content_sha256="seal",
            run_id=run.name,
            phase="finished",
            side_effect=result.side_effect,
            result=result.model_dump(mode="json"),
        ),
    )
    return run


def test_cleanup_is_scoped_dry_by_default_and_preserves_retry_authority(tmp_path: Path) -> None:
    run = completed(tmp_path)
    other = completed(tmp_path, "other")
    before = SQLiteInvocationStore.existing(tmp_path).records()
    report = inspect(tmp_path, "local")
    assert report["candidates"] == ["run_local/screenshots/0001.png"]
    assert report["bytes_eligible"] == 5
    assert (run / "screenshots/0001.png").exists()
    assert not (tmp_path / ".maintenance.jsonl").exists()
    assert inspect(tmp_path, "local", apply=True)["deleted"] == 1
    assert (other / "screenshots/0001.png").exists()
    assert (run / "log.jsonl").exists() and (run / "result.json").exists()
    assert SQLiteInvocationStore.existing(tmp_path).records() == before
    assert inspect(tmp_path, "local", apply=True)["deleted"] == 0


@pytest.mark.parametrize("reason", ["unknown", "handoff", "active", "recent"])
def test_protected_evidence_is_preserved(tmp_path: Path, reason: str) -> None:
    run = completed(tmp_path, unknown=reason == "unknown")
    if reason == "handoff":
        (run / "handoff_state.json").write_text("{}", encoding="utf-8")
    if reason == "recent":
        os.utime(run / "result.json", None)
    if reason == "active":
        with execution_lock(tmp_path, identity("local", "key")):
            report = inspect(tmp_path, "local", apply=True)
            assert report["cleanup_failures"] == 1
            assert report["deleted"] == 0
        return
    assert inspect(tmp_path, "local", apply=True)["deleted"] == 0


def test_corrupt_or_missing_authority_never_becomes_empty_state(tmp_path: Path) -> None:
    completed(tmp_path)
    (tmp_path / ".invocations/invocations.sqlite3").unlink()
    with pytest.raises(ValueError):
        inspect(tmp_path, "local", apply=True)
    assert not (tmp_path / ".invocations/invocations.sqlite3").exists()


def test_missing_and_legacy_roots_are_read_only(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert inspect(missing, "local")["candidates"] == []
    assert not missing.exists()
    run = tmp_path / "run_legacy"
    run.mkdir()
    assert inspect(tmp_path, "local", apply=True)["preserved_runs"] == 1
    assert list(tmp_path.iterdir()) == [run]


def test_traversal_is_rejected(tmp_path: Path) -> None:
    assert not safe_path(tmp_path, tmp_path / "..")
    assert not safe_path(tmp_path, tmp_path.parent)


def test_symlink_screenshots_are_preserved(tmp_path: Path) -> None:
    run = completed(tmp_path)
    shots = run / "screenshots"
    (shots / "0001.png").unlink()
    shots.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.png").write_bytes(b"private")
    try:
        shots.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    assert inspect(tmp_path, "local", apply=True)["deleted"] == 0
    assert (outside / "private.png").exists()


def test_cli_dry_run_and_invalid_retention(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    completed(tmp_path)
    args = ["maintenance", "cleanup", "--runs-dir", str(tmp_path)]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert main([*args, "--retention-days", "nan"]) == 1


def test_result_mismatch_stops_cleanup(tmp_path: Path) -> None:
    run = completed(tmp_path)
    data = json.loads((run / "result.json").read_text(encoding="utf-8"))
    data["outputs"] = {"changed": "value"}
    atomic_write_json(run / "result.json", data)
    with pytest.raises(ValueError, match="differs"):
        inspect(tmp_path, "local", apply=True)
    assert (run / "screenshots/0001.png").exists()


def test_mismatched_tenant_header_is_preserved(tmp_path: Path) -> None:
    run = completed(tmp_path)
    atomic_write_json(run / "run.json", {"kind": "replay", "tenant": {"id": "other"}})
    assert inspect(tmp_path, "local", apply=True)["deleted"] == 0
