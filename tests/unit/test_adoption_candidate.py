"""Candidate builds use committed source and cannot manufacture release readiness."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts.ci.check_adoption_release import GATES, check
from scripts.ci.prepare_adoption_candidate import extract_snapshot, prepare


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    files = {
        "src/cua/__init__.py": '__version__ = "0.1.0"\n',
        "docs/release/trial-template.md": "Status: pending. Real participants required.\n",
        "scripts/ci/check_distribution.py": "print('synthetic distribution fixture')\n",
        "scripts/ci/verify_installed.py": "print('synthetic installed fixture')\n",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for args in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "candidate fixture",
        ],
    ):
        subprocess.run(args, cwd=root, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    return root, commit


def fake_build(monkeypatch, *, fail=None):
    original = subprocess.run

    def run(args, **kwargs):
        if args[1:3] == ["-m", "build"]:
            # Inspect the actual git archive snapshot; workspace edits must not
            # leak into a build that claims the earlier committed identity.
            source = Path(kwargs["cwd"])
            assert (source / "src/cua/__init__.py").read_text() == '__version__ = "0.1.0"\n'
            assert not (source / "private-untracked.txt").exists()
            if fail == "build":
                raise subprocess.CalledProcessError(1, args)
            dist = Path(args[args.index("--outdir") + 1])
            dist.mkdir()
            with zipfile.ZipFile(dist / "inter_cua-0.1.0-py3-none-any.whl", "w") as archive:
                archive.writestr(
                    "inter_cua-0.1.0.dist-info/METADATA", "Name: inter-cua\nVersion: 0.1.0\n"
                )
            (dist / "inter_cua-0.1.0.tar.gz").write_bytes(b"synthetic sdist fixture")
            return subprocess.CompletedProcess(args, 0)
        if fail == "installed" and str(args[1]).endswith("verify_installed.py"):
            raise subprocess.CalledProcessError(1, args)
        return original(args, **kwargs)

    monkeypatch.setattr("scripts.ci.prepare_adoption_candidate.subprocess.run", run)


def test_candidate_is_bound_to_committed_source_and_keeps_all_reviews_pending(
    repository,
    tmp_path,
    monkeypatch,
):
    root, commit = repository
    (root / "src/cua/__init__.py").write_text("uncommitted changes")
    (root / "private-untracked.txt").write_text("must stay outside candidate")
    fake_build(monkeypatch)
    out = tmp_path / "bundle"
    summary = prepare(root, out, commit, verify_installed=True)
    assert summary["status"] == "candidate_prepared"
    assert summary["release_status"] == "not_qualified"
    record = json.loads((out / "record.json").read_text())
    assert record["commit"] == commit and record["version"] == "0.1.0"
    assert set(record["gates"]) == set(GATES)
    assert all(gate["status"] == "pending" for gate in record["gates"].values())
    assert record["trials"] == [] and record["open_blockers"]
    assert len(list((out / "trials").glob("*.md"))) == 2
    provenance = json.loads((out / "provenance.json").read_text())
    assert provenance["commit"] == commit
    assert set(provenance["checks"].values()) == {"passed"}
    assert set(provenance["checks"]) == {
        "snapshot",
        "build",
        "distribution",
        "installed-wheel",
        "installed-sdist",
    }
    assert provenance["wheel"] == record["wheel"]
    with pytest.raises(ValueError, match="no passing reviewed evidence"):
        check(out / "record.json")
    assert (root / "src/cua/__init__.py").read_text() == "uncommitted changes"


@pytest.mark.parametrize("stage", ["build", "installed"])
def test_failure_preserves_failed_status_and_does_not_claim_readiness(
    repository,
    tmp_path,
    monkeypatch,
    stage,
):
    root, commit = repository
    fake_build(monkeypatch, fail=stage)
    out = tmp_path / "failed"
    with pytest.raises(subprocess.CalledProcessError):
        prepare(root, out, commit, verify_installed=True)
    assert json.loads((out / "summary.json").read_text())["status"] == "failed"
    provenance = json.loads((out / "provenance.json").read_text())
    assert "failed" in provenance["checks"].values()
    assert json.loads((out / "summary.json").read_text())["release_status"] == "not_qualified"


def test_existing_evidence_cannot_be_overwritten(repository, tmp_path):
    root, commit = repository
    with pytest.raises(FileExistsError):
        prepare(root, tmp_path, commit)


def test_cleanup_failure_cannot_leave_a_success_summary(repository, tmp_path, monkeypatch):
    import tempfile

    root, commit = repository
    fake_build(monkeypatch)
    temporary = tempfile.TemporaryDirectory

    @contextmanager
    def fail_cleanup(**kwargs):
        with temporary(**kwargs) as workspace:
            yield workspace
        raise OSError("cleanup failed")

    monkeypatch.setattr(
        "scripts.ci.prepare_adoption_candidate.tempfile.TemporaryDirectory", fail_cleanup
    )
    out = tmp_path / "cleanup-failed"
    with pytest.raises(OSError, match="cleanup failed"):
        prepare(root, out, commit)
    summary = json.loads((out / "summary.json").read_text())
    assert summary["status"] == "failed"
    assert "cleanup failed" in summary["error"]


@pytest.mark.parametrize("commit", ["main", "HEAD", "a" * 39, "A" * 40, "a" * 40 + "--option"])
def test_symbolic_or_malformed_commit_is_refused_before_output(tmp_path, commit):
    out = tmp_path / "bundle"
    with pytest.raises(ValueError, match="full reviewed"):
        prepare(tmp_path, out, commit)
    assert not out.exists()


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", "file"),
        ("/escape", "file"),
        ("C:/escape", "file"),
        ("sub\\escape", "file"),
        ("link", "symlink"),
        ("hard", "hardlink"),
    ],
)
def test_unsafe_snapshot_members_are_refused_before_extraction(tmp_path, name, kind):
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as stream:
        member = tarfile.TarInfo(name)
        if kind == "file":
            member.size = 4
            stream.addfile(member, io.BytesIO(b"data"))
        else:
            member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            member.linkname = "../outside"
            stream.addfile(member)
    target = tmp_path / "source"
    target.mkdir()
    with pytest.raises(ValueError, match="unsupported snapshot member"):
        extract_snapshot(archive, target)
    assert not list(target.iterdir())


def test_cli_refuses_invalid_commit_without_traceback(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.ci.prepare_adoption_candidate",
            "--commit",
            "main",
            "--out",
            str(tmp_path / "bundle"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "adoption candidate preparation failed:" in result.stderr
    assert "Traceback" not in result.stderr
