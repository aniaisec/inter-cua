"""The capability registry: versions that coexist, a lifecycle that only moves
the ways it may, a catalog that is a view of it, and health from runs.

Nothing here starts a browser: a replay that would get past the lifecycle
check fails the test the moment it asks for one.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cua import catalog
from cua.artifact.schema import Capability
from cua.artifact.store import load, save, with_changes
from cua.cli import main
from cua.policy.allowlist import load_policy
from cua.policy.approval import ApprovalRefused, approve, record_review
from cua.registry import lifecycle
from cua.registry.health import health_by_version, replays_of
from cua.registry.resolver import Unresolvable, resolve
from cua.registry.store import Registry, RegistryError
from cua.replay.invocation import Invocation
from cua.replay.result import Failure, IdempotencyCache, fingerprint
from cua.replay.runner import replay
from cua.tenant import Tenant
from tests.conftest import REPO_ROOT

CAPS = REPO_ROOT / "capabilities"
GOAL1 = CAPS / "member_savings_balance.json"
GOAL2 = CAPS / "open_subaccount.json"
NAME = "member_savings_balance"
TENANT = Tenant(id="local", app_family="legacy-core", base_url="http://127.0.0.1:8000")
POLICY = load_policy(REPO_ROOT / "policies" / "default.yaml", TENANT)


@pytest.fixture
def caps(tmp_path: Path) -> Path:
    """The two committed capabilities, approved, not yet registered."""
    out = tmp_path / "capabilities"
    out.mkdir()
    for src in (GOAL1, GOAL2):
        shutil.copy(src, out / src.name)
    return out


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def registry(caps: Path, state: Path) -> Registry:
    return Registry(caps, state_dir=state, tenants_dir=REPO_ROOT / "tenants")


def new_version(path: Path, state: Path, *, description: str, by: str = "anil") -> Capability:
    """Re-record the capability with a change, then describe and approve it."""
    save(with_changes(load(path), description=description), path)
    record_review(load(path), path, state_dir=state)
    return approve(path, by, state_dir=state)


def no_browser() -> None:
    raise AssertionError("a browser was started")


def run_replay(path: Path, tmp_path: Path, **invocation: object) -> object:
    return replay(
        path,
        tenant=TENANT,
        policy=POLICY,
        invocation=Invocation(inputs={"member_id": "10001"}, **invocation),  # type: ignore[arg-type]
        runs_dir=tmp_path / "runs",
        surface=no_browser,  # type: ignore[arg-type]
    )


def cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


# -- identity and versions -------------------------------------------------------------


def test_an_approved_working_copy_is_a_version_before_anything_is_registered(
    caps: Path, state: Path
) -> None:
    (v,) = registry(caps, state).versions(NAME)
    r = v.record
    assert (r.name, r.version, r.status, r.app_family) == (NAME, 3, "approved", "legacy-core")
    assert r.tenant_scope == ["local"]
    assert r.working_copy and not r.registered
    assert r.artifact_hash == load(GOAL1).content_hash()
    assert r.history == []


def test_sync_registers_approved_capabilities_once(caps: Path, state: Path) -> None:
    reg = registry(caps, state)
    written = reg.sync()
    assert sorted(p.relative_to(caps).as_posix() for p in written) == [
        "registry/member_savings_balance/v3.json",
        "registry/open_subaccount/v3.json",
    ]
    assert reg.sync() == []  # nothing left to do
    (v,) = reg.versions(NAME)
    assert v.record.registered and v.record.working_copy
    (entry,) = v.record.history
    assert (entry.previous, entry.status, entry.by) == ("review", "approved", "anil")
    assert entry.at == load(GOAL1).approved_at
    assert (caps / "registry" / NAME / "v3.json").read_bytes() == GOAL1.read_bytes()


def test_versions_coexist_after_the_capability_is_re_recorded(
    caps: Path, state: Path, tmp_path: Path
) -> None:
    path = caps / f"{NAME}.json"
    approve(path, "anil", state_dir=state)  # registers the approved v3
    save(with_changes(load(path), description="Look up a balance, v4"), path)

    reg = registry(caps, state)
    v3, v4 = reg.versions(NAME)
    assert (v3.record.version, v3.status, v3.record.registered) == (3, "approved", True)
    assert not v3.record.working_copy
    assert (v4.record.version, v4.status, v4.record.working_copy) == (4, "draft", True)

    # By name, the approved v3 still runs, from its registered copy.
    assert resolve(reg, NAME).path == caps / "registry" / NAME / "v3.json"
    (entry,) = [e for e in catalog.scan(caps)[0] if e.capability.name == NAME]
    assert (entry.capability.version, entry.invocable, entry.versions) == (3, True, 2)

    # Describe and approve v4: it becomes the default, and v3 is still there.
    record_review(load(path), path, state_dir=state)
    approve(path, "anil", state_dir=state)
    assert resolve(reg, NAME).capability.version == 4
    assert resolve(reg, NAME, 3).path == caps / "registry" / NAME / "v3.json"
    assert [v.status for v in reg.versions(NAME)] == ["approved", "approved"]
    with pytest.raises(Unresolvable, match=r"has no v9 \(known: v3, v4\)"):
        resolve(reg, NAME, 9)


def test_the_registered_copy_is_what_runs_and_it_is_never_replaced(caps: Path, state: Path) -> None:
    reg = registry(caps, state)
    reg.sync()
    other = with_changes(load(GOAL1), description="something else")
    other = with_changes(other, content_sha256=other.content_hash())
    with pytest.raises(RegistryError, match="never replaced"):
        reg.register(other)


def test_a_registered_copy_edited_by_hand_is_not_a_version(caps: Path, state: Path) -> None:
    reg = registry(caps, state)
    reg.sync()
    (caps / f"{NAME}.json").unlink()
    snapshot = caps / "registry" / NAME / "v3.json"
    snapshot.write_text(
        snapshot.read_text(encoding="utf-8").replace("savings balance", "balance"),
        encoding="utf-8",
    )
    assert reg.versions(NAME) == []
    assert [u.error for u in reg.unreadable] == [
        "changed after it was registered; restore it from git"
    ]


# -- lifecycle --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "to", "ok"),
    [
        ("draft", "review", True),
        ("review", "approved", True),
        ("approved", "deprecated", True),
        ("approved", "revoked", True),
        ("deprecated", "approved", True),
        ("deprecated", "revoked", True),
        ("draft", "approved", False),
        ("draft", "deprecated", False),
        ("review", "revoked", False),
        ("revoked", "approved", False),
        ("revoked", "deprecated", False),
        ("approved", "approved", False),
        ("approved", "draft", False),
    ],
)
def test_only_the_lifecycle_transitions_are_allowed(
    current: lifecycle.Status, to: lifecycle.Status, ok: bool
) -> None:
    if ok:
        lifecycle.check(current, to)
    else:
        with pytest.raises(lifecycle.LifecycleError):
            lifecycle.check(current, to)


def test_the_ledger_can_never_approve_what_the_artifact_does_not(caps: Path) -> None:
    from cua.registry.models import LedgerEntry

    reinstated = LedgerEntry(
        name=NAME,
        version=1,
        capability_id="cap_x",
        artifact_hash="0" * 64,
        previous="deprecated",
        status="approved",
        by="x",
        at="2026-09-25T00:00:00Z",
    )
    assert lifecycle.fold("draft", [reinstated]) == "draft"
    assert lifecycle.fold("approved", [reinstated]) == "approved"


def test_deprecate_reinstate_and_revoke_are_recorded_with_who_and_why(
    caps: Path, state: Path
) -> None:
    reg = registry(caps, state)
    r = reg.transition(NAME, 3, "deprecate", by="ops", reason="superseded")
    assert r.status == "deprecated" and r.registered  # a decision is about a kept version
    assert reg.transition(NAME, 3, "reinstate", by="ops").status == "approved"
    r = reg.transition(NAME, 3, "revoke", by="sec", reason="reads the wrong account")
    assert r.status == "revoked"
    assert [(e.previous, e.status, e.by) for e in r.history] == [
        ("review", "approved", "anil"),
        ("approved", "deprecated", "ops"),
        ("deprecated", "approved", "ops"),
        ("approved", "revoked", "sec"),
    ]
    lines = (caps / "registry" / "lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["reason"] == "reads the wrong account"

    with pytest.raises(RegistryError, match="revoked, which is final"):
        reg.transition(NAME, 3, "reinstate", by="ops")


def test_a_revocation_needs_a_reason_and_a_name(caps: Path, state: Path) -> None:
    reg = registry(caps, state)
    with pytest.raises(RegistryError, match="say why"):
        reg.transition(NAME, 3, "revoke", by="sec")
    with pytest.raises(RegistryError, match="say who"):
        reg.transition(NAME, 3, "deprecate", by=" ")
    with pytest.raises(RegistryError, match="has no v7"):
        reg.transition(NAME, 7, "deprecate", by="ops")


def test_a_draft_cannot_be_deprecated_and_a_draft_awaiting_approval_is_in_review(
    caps: Path, state: Path
) -> None:
    path = caps / f"{NAME}.json"
    save(with_changes(load(path), description="v4"), path)
    reg = registry(caps, state)
    assert reg.version(NAME, 4).status == "draft"
    with pytest.raises(RegistryError, match="a draft version can only become review"):
        reg.transition(NAME, 4, "deprecate", by="ops")
    record_review(load(path), path, state_dir=state)
    assert reg.version(NAME, 4).status == "review"


def test_approve_will_not_bring_back_a_retired_version(caps: Path, state: Path) -> None:
    path = caps / f"{NAME}.json"
    reg = registry(caps, state)
    reg.transition(NAME, 3, "deprecate", by="ops")
    with pytest.raises(ApprovalRefused, match="cua registry reinstate"):
        approve(path, "anil", state_dir=state)
    reg.transition(NAME, 3, "revoke", by="sec", reason="bad")
    with pytest.raises(ApprovalRefused, match="revoked, which is final"):
        approve(path, "anil", state_dir=state)


# -- who may start ----------------------------------------------------------------------


def test_the_default_skips_deprecated_and_revoked_versions(caps: Path, state: Path) -> None:
    path = caps / f"{NAME}.json"
    reg = registry(caps, state)
    approve(path, "anil", state_dir=state)
    new_version(path, state, description="v4")
    assert resolve(reg, NAME).capability.version == 4

    reg.transition(NAME, 4, "deprecate", by="ops")
    assert resolve(reg, NAME).capability.version == 3
    reg.transition(NAME, 3, "revoke", by="sec", reason="bad")
    # Nothing approved: the highest is returned, for replay to refuse with a result.
    assert resolve(reg, NAME).capability.version == 4
    (entry,) = [e for e in catalog.scan(caps)[0] if e.capability.name == NAME]
    assert entry.status == "deprecated" and not entry.invocable
    assert NAME not in [t["name"] for t in catalog.tools(catalog.scan(caps)[0])]


def test_a_revoked_version_starts_no_run(caps: Path, state: Path, tmp_path: Path) -> None:
    registry(caps, state).transition(NAME, 3, "revoke", by="sec", reason="reads the wrong row")
    for path in (caps / f"{NAME}.json", caps / "registry" / NAME / "v3.json"):
        result = run_replay(path, tmp_path)
        assert isinstance(result, Failure)
        assert result.code == "POLICY_BLOCKED" and result.side_effect == "none"
        assert "revoked and starts no new execution" in result.message
    assert not (tmp_path / "runs").exists() or not any((tmp_path / "runs").glob("run_*"))


def test_a_retry_of_a_run_that_already_happened_still_gets_its_answer(
    caps: Path, state: Path, tmp_path: Path
) -> None:
    """In-flight policy: the idempotency cache answers before the lifecycle is
    asked, because a stored result starts nothing."""
    path = caps / f"{NAME}.json"
    stored = Failure(
        code="TIMEOUT", message="stored", side_effect="unknown", capability=NAME,
        capability_version=3, idempotency_key="k1",
    )  # fmt: skip
    IdempotencyCache(tmp_path / "runs").put(
        "k1", fingerprint(NAME, 3, {"member_id": "10001"}), stored
    )
    registry(caps, state).transition(NAME, 3, "revoke", by="sec", reason="bad")
    result = run_replay(path, tmp_path, idempotency_key="k1")
    assert isinstance(result, Failure) and result.message == "stored"
    fresh = run_replay(path, tmp_path, idempotency_key="k2")
    assert isinstance(fresh, Failure) and "revoked" in fresh.message


def test_an_unreadable_ledger_fails_closed(caps: Path, state: Path, tmp_path: Path) -> None:
    ledger = caps / "registry" / "lifecycle.jsonl"
    ledger.parent.mkdir()
    ledger.write_text('{"name": "member_savings_balance", "status": "revo', encoding="utf-8")
    result = run_replay(caps / f"{NAME}.json", tmp_path)
    assert isinstance(result, Failure) and result.code == "POLICY_BLOCKED"
    assert result.message.startswith("lifecycle unknown:")


# -- the catalog is a view of the registry ----------------------------------------------


def test_catalog_and_registry_agree(caps: Path, state: Path) -> None:
    path = caps / f"{NAME}.json"
    approve(path, "anil", state_dir=state)
    save(with_changes(load(path), description="v4"), path)
    registry(caps, state).transition("open_subaccount", 3, "deprecate", by="ops")

    entries, _ = catalog.scan(caps)
    reg = registry(caps, state)
    for e in entries:
        default = resolve(reg, e.capability.name)
        assert (e.capability.version, e.status, e.path) == (
            default.capability.version,
            default.status,
            default.path,
        )
    assert [(e.capability.name, e.capability.version, e.status) for e in entries] == [
        (NAME, 3, "approved"),
        ("open_subaccount", 3, "deprecated"),
    ]
    assert [t["name"] for t in catalog.tools(entries)] == [NAME]
    assert catalog.find(caps, NAME) == caps / "registry" / NAME / "v3.json"
    assert catalog.find(caps, NAME, 4) == path


def test_catalog_invoke_runs_the_version_asked_for(
    caps: Path, state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry(caps, state).transition(NAME, 3, "revoke", by="sec", reason="bad")
    argv = ["catalog", "invoke", NAME, "--capabilities-dir", str(caps)]
    argv += ["--args", '{"member_id": "10001"}', "--runs-dir", str(tmp_path / "runs")]
    code, out, _ = cli(argv, capsys)
    assert code == 1 and json.loads(out)["code"] == "POLICY_BLOCKED"
    code, _, err = cli([*argv, "--version", "5"], capsys)
    assert code == 64 and "has no v5 (known: v3)" in err


# -- health comes from runs -------------------------------------------------------------


def test_health_is_observed_or_absent_never_invented() -> None:
    runs = replays_of(NAME, [REPO_ROOT / "evidence"])
    assert runs and all(r.capability == NAME and r.kind == "replay" for r in runs)
    health = health_by_version(NAME, runs)
    assert health and all(h.runs > 0 for h in health.values())
    assert health_by_version("no_such_capability", replays_of("no_such_capability")) == {}


# -- the CLI ----------------------------------------------------------------------------


def test_the_registry_commands(
    caps: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = ["--capabilities-dir", str(caps)]
    code, out, _ = cli(["registry", "list", *d], capsys)
    assert code == 0 and "v3  approved   [default, working copy] by anil" in out

    code, out, _ = cli(["registry", "sync", *d], capsys)
    assert code == 0 and out.count("registered") == 2

    code, out, _ = cli(
        ["registry", "deprecate", NAME, "--version", "3", "--by", "ops", "--reason", "old", *d],
        capsys,
    )
    assert code == 0 and "is deprecated" in out
    code, out, _ = cli(["registry", "versions", NAME, *d], capsys)
    assert "v3  deprecated [registered + working copy]" in out and "deprecated by ops" in out

    code, _, err = cli(["registry", "revoke", NAME, "--version", "3", "--by", "sec", *d], capsys)
    assert code == 1 and "say why" in err
    code, out, _ = cli(
        ["registry", "revoke", NAME, "--version", "3", "--by", "sec", "--reason", "x", *d],
        capsys,
    )
    assert code == 0 and "is revoked" in out

    runs = ["--runs-dir", str(REPO_ROOT / "evidence")]
    code, out, _ = cli(["registry", "show", NAME, "--json", *d, *runs], capsys)
    shown = json.loads(out)
    assert code == 0 and shown["status"] == "revoked" and shown["health"]["runs"] > 0

    code, _, err = cli(["registry", "show", "nope", *d], capsys)
    assert code == 64 and "no capability named 'nope'" in err
    code, out, _ = cli(["registry", "health", NAME, *d, *runs], capsys)
    assert code == 0 and "v3  revoked" in out and "| Capability | Version |" in out


def test_the_committed_capabilities_are_registered_and_approved() -> None:
    """Backward compatibility: what was approved before the registry is
    approved in it, and the registered copy is the committed file."""
    reg = Registry(CAPS, tenants_dir=REPO_ROOT / "tenants")
    for name, path in ((NAME, GOAL1), ("open_subaccount", GOAL2)):
        v = resolve(reg, name)
        assert v.status == "approved" and v.record.registered and v.record.working_copy
        assert v.capability.content_hash() == load(path).content_hash()
    assert reg.unreadable == []
