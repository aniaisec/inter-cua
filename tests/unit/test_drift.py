"""Drift: classified from run directories, counted into rates, and turned into
a candidate repair that nothing runs until a person approves it.

The renamed-button run is the committed handoff evidence
(``evidence/replay-resume-after-human``): member_savings_balance v3, the
Search button injected as Find, the failure screen kept. Other shapes are
built as run directories here. Nothing starts a browser.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from cua.artifact.store import load, with_changes
from cua.cli import main
from cua.drift.aggregate import aggregate
from cua.drift.candidate import propose
from cua.drift.checks import static_checks
from cua.drift.detect import Known, at_recorded_place, read_drift, scan, screen_of
from cua.drift.evaluate import _gates, tasks_for
from cua.drift.models import (
    Control,
    DriftEvent,
    Evaluation,
    Gate,
    RunDrift,
    SideResult,
    TaskResult,
)
from cua.drift.store import CandidateError, Candidates, approval_refusal, status
from cua.observability.recorder import parse_attempts, read_run
from cua.policy.allowlist import load_policy
from cua.policy.approval import ApprovalRefused, approve, record_review
from cua.registry.resolver import resolve
from cua.registry.store import Registry
from cua.surface.locators import resolve_ladder
from cua.surface.protocol import Observation
from cua.tenant import Tenant
from tests.conftest import REPO_ROOT

RENAMED = REPO_ROOT / "evidence" / "replay-resume-after-human"
NAME = "member_savings_balance"
TENANT = Tenant(id="local", app_family="legacy-core", base_url="http://127.0.0.1:8000")
POLICY = load_policy(REPO_ROOT / "policies" / "default.yaml", TENANT)
T0 = "2026-09-25T10:00:00.000Z"


@pytest.fixture
def caps(tmp_path: Path) -> Path:
    """The committed capabilities and registry, copied."""
    out = tmp_path / "capabilities"
    shutil.copytree(REPO_ROOT / "capabilities", out)
    return out


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def run(tmp_path: Path) -> Path:
    """The renamed-button run, copied, so a test can point at it or alter it."""
    out = tmp_path / "runs" / "renamed"
    shutil.copytree(RENAMED, out)
    return out


def known() -> Known:
    return Known(v.capability for v in Registry(REPO_ROOT / "capabilities").all_versions())


def _ts(seconds: float) -> str:
    return f"2026-09-25T10:00:{int(seconds):02d}.{round((seconds % 1) * 1000):03d}Z"


def built_run(
    root: Path,
    log: list[dict[str, Any]],
    *,
    run_id: str = "run_BUILT",
    inject: str | None = None,
    key: str | None = None,
    tenant: str = "t1",
    kind: str = "replay",
) -> Path:
    d = root / run_id
    d.mkdir(parents=True)
    header: dict[str, Any] = (
        {
            "run_id": run_id,
            "kind": "replay",
            "started_at": T0,
            "capability": {"id": "cap_X", "name": "cap_x", "version": 2},
            "tenant": {"id": tenant, "app_family": "legacy-core"},
            "request": {"inputs": {}, "idempotency_key": key, "inject": inject},
        }
        if kind == "replay"
        else {"run_id": run_id, "goal": {"name": "g", "goal": "do it"}, "started_at": T0}
    )
    (d / "run.json").write_text(json.dumps(header), encoding="utf-8")
    lines = [json.dumps({"seq": i + 1, **line}) for i, line in enumerate(log)]
    (d / "log.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return d


def step(name: str, at: float) -> dict[str, Any]:
    return {"ts": _ts(at), "event": "step.start", "step": name, "action": "click"}


def resolved(target: str, at: float, rung: str, recorded: str, attempts: list[Any]) -> Any:
    return {
        "ts": _ts(at),
        "event": "locator.resolved",
        "target": target,
        "rung": rung,
        "recorded": recorded,
        "attempts": attempts,
    }


def failed(at: float, code: str, step_id: str, **extra: Any) -> dict[str, Any]:
    return {"ts": _ts(at), "event": "replay.failed", "code": code, "step": step_id, **extra}


def tree_hash(root: Path, *, skip: str = "candidates") -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and skip not in p.relative_to(root).parts:
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


# -- classification -------------------------------------------------------------------


def test_the_committed_renamed_button_run_is_a_control_renamed() -> None:
    drift = read_drift(RENAMED, known())
    assert drift is not None and drift.reached_app
    [event] = drift.events
    assert event.kind == "CONTROL_RENAMED" and event.fatal
    assert (event.capability, event.version, event.step_id) == (NAME, 3, "search.submit")
    assert event.expected_locator_rung == "role_name"
    assert event.observed_rungs == ["role_name=0", "bbox=1 untrusted"]
    assert event.reason == "the button at the recorded place is now 'Find', recorded as 'Search'"
    assert event.injected == "renamed_button"
    assert event.tenant_id == "local" and event.app_family == "legacy-core"
    assert event.evidence_ref is not None and event.evidence_ref.endswith("observations/0005.json")
    assert "failure screen" in event.basis
    assert drift.lookups["role_name"] == 2  # login.submit, and search.submit that failed


def test_without_the_version_on_disk_it_is_classified_from_the_rungs_alone() -> None:
    drift = read_drift(RENAMED, Known())
    assert drift is not None
    [event] = drift.events
    assert event.kind == "CONTROL_RENAMED"
    assert event.basis == "rung attempts read from the failure message"


def test_the_failure_message_is_read_back_into_attempts() -> None:
    assert parse_attempts(
        "during recovery (session_expired at login.submit): role_name: 2 matches; "
        "near_text: refused (viewport differs); bbox: 1 match, not trusted to act on "
        "unattended (pixels only)"
    ) == [
        {"rung": "role_name", "matches": 2},
        {"rung": "near_text", "matches": 0, "refused": "viewport differs"},
        {"rung": "bbox", "matches": 1, "untrusted": True},
    ]


def test_logged_attempts_classify_ambiguous_and_missing_controls(tmp_path: Path) -> None:
    ambiguous = built_run(
        tmp_path,
        [
            step("a.click", 1),
            failed(
                2,
                "LOCATOR_AMBIGUOUS",
                "a.click",
                attempts=[
                    {"rung": "role_name", "matches": 2, "refs": ["n1", "n2"]},
                    {"rung": "bbox", "matches": 0},
                ],
            ),
        ],
        run_id="run_AMB",
    )
    missing = built_run(
        tmp_path,
        [
            step("a.click", 1),
            failed(
                2,
                "LOCATOR_UNRESOLVED",
                "a.click",
                attempts=[{"rung": "role_name", "matches": 0}, {"rung": "bbox", "matches": 0}],
            ),
        ],
        run_id="run_MISS",
    )
    [amb] = read_drift(ambiguous).events  # type: ignore[union-attr]
    assert amb.kind == "CONTROL_AMBIGUOUS" and amb.observed_rungs == ["role_name=2", "bbox=0"]
    assert amb.basis == "rung attempts logged"
    [gone] = read_drift(missing).events  # type: ignore[union-attr]
    assert gone.kind == "CONTROL_MISSING" and gone.fatal
    # An ambiguous control is a locator failure in the canonical events too.
    assert read_run(ambiguous).of("locator.failed")


def test_drift_a_run_survived_is_recorded_as_not_fatal(tmp_path: Path) -> None:
    d = built_run(
        tmp_path,
        [
            step("a.click", 1),
            resolved(
                "a.click",
                2,
                "near_text",
                "role_name",
                [{"rung": "role_name", "matches": 0}, {"rung": "near_text", "matches": 1}],
            ),
            resolved(  # the same slip again on a retry is one drift
                "a.click",
                2.5,
                "near_text",
                "role_name",
                [{"rung": "role_name", "matches": 0}, {"rung": "near_text", "matches": 1}],
            ),
            step("b.type", 3),
            resolved(
                "b.type",
                4,
                "bbox",
                "near_text",
                [{"rung": "near_text", "matches": 0}, {"rung": "bbox", "matches": 1}],
            ),
            resolved(
                "outputs.balance",
                5,
                "near_text",
                "table_cell",
                [{"rung": "table_cell", "matches": 0}, {"rung": "near_text", "matches": 1}],
            ),
            resolved("c.click", 6, "role_name", "role_name", [{"rung": "role_name", "matches": 1}]),
        ],
    )
    drift = read_drift(d)
    assert drift is not None
    kinds = [(e.step_id, e.kind, e.fatal, e.resolved_rung) for e in drift.events]
    assert kinds == [
        ("a.click", "CONTROL_RENAMED", False, "near_text"),
        ("b.type", "LAYOUT_CHANGED", False, "bbox"),
        ("outputs.balance", "OUTPUT_CHANGED", False, "near_text"),
    ]
    assert drift.lookups == {"role_name": 3, "near_text": 1, "table_cell": 1}


def test_an_output_or_checkpoint_that_no_longer_holds_is_drift(tmp_path: Path) -> None:
    d = built_run(
        tmp_path,
        [
            step("a.click", 1),
            failed(
                2,
                "EXTRACTION_FAILED",
                "a.click",
                expected='output balance: exactly one node for table_cell "Savings" x "Balance"',
            ),
            failed(3, "CHECKPOINT_FAILED", "a.click", message="a.click arrived somewhere"),
        ],
    )
    drift = read_drift(d)
    assert drift is not None
    got = [(e.step_id, e.kind, e.expected_locator_rung) for e in drift.events]
    assert got == [
        ("outputs.balance", "OUTPUT_CHANGED", "table_cell"),
        ("a.click", "CHECKPOINT_CHANGED", "-"),
    ]


def test_a_clean_run_has_no_drift_and_a_discovery_run_is_not_read(tmp_path: Path) -> None:
    clean = built_run(
        tmp_path,
        [
            step("a.click", 1),
            resolved("a.click", 2, "role_name", "role_name", [{"rung": "role_name", "matches": 1}]),
        ],
        run_id="run_CLEAN",
    )
    refused = built_run(tmp_path, [{"ts": _ts(1), "event": "run.end"}], run_id="run_REFUSED")
    discovery = built_run(tmp_path, [], run_id="run_DISC", kind="discovery")
    got = read_drift(clean)
    assert got is not None and got.events == [] and got.reached_app
    assert read_drift(refused).reached_app is False  # type: ignore[union-attr]
    assert read_drift(discovery) is None


def _with_screen(run: Path, edit: Any, log: list[dict[str, Any]] | None = None) -> Path:
    """The renamed-button run with its failure screen changed (and its log,
    if given)."""
    obs_path = run / "observations" / "0005.json"
    obs = json.loads(obs_path.read_text(encoding="utf-8"))
    edit(obs)
    obs_path.write_text(json.dumps(obs), encoding="utf-8")
    if log is not None:
        lines = [json.dumps(line) for line in log]
        (run / "log.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run


def test_a_frame_that_is_gone_is_a_frame_change(run: Path) -> None:
    def rename_main(obs: dict[str, Any]) -> None:
        for f in obs["frames"]:
            if f["name"] == "main":
                f["name"] = "content"
        for n in obs["nodes"]:
            if n["frame"] == "main":
                n["frame"] = "content"

    [event] = read_drift(_with_screen(run, rename_main), known()).events  # type: ignore[union-attr]
    assert event.kind == "FRAME_CHANGED"
    assert event.reason == "the 'main' frame it is scoped to is not on the screen"


def test_something_else_at_the_recorded_place_is_a_missing_control(run: Path) -> None:
    def link_instead(obs: dict[str, Any]) -> None:
        for n in obs["nodes"]:
            if n["role"] == "button" and n["name"] == "Find":
                n["role"] = "link"

    [event] = read_drift(_with_screen(run, link_instead), known()).events  # type: ignore[union-attr]
    assert event.kind == "CONTROL_MISSING"
    assert event.reason == "no button is where it was recorded on the kept screen"


def test_a_step_that_lands_elsewhere_is_a_navigation_change(run: Path) -> None:
    log = [
        {"seq": 1, "ts": _ts(1), "event": "step.start", "step": "login.submit"},
        {
            "seq": 2,
            "ts": _ts(2),
            "event": "replay.failed",
            "code": "CHECKPOINT_FAILED",
            "step": "login.submit",
            "message": "login.submit arrived somewhere, but not at cp.logged_in",
        },
        {
            "seq": 3,
            "ts": _ts(2.5),
            "event": "observe",
            "label": "login.submit.failed",
            "observation": "observations/0005.json",
        },
    ]

    def to_notice(obs: dict[str, Any]) -> None:
        for f in obs["frames"]:
            if f["name"] == "main":
                f["url"] = "http://127.0.0.1:8000/notice?next=%2Fsearch"

    [event] = read_drift(_with_screen(run, to_notice, log), known()).events  # type: ignore[union-attr]
    assert event.kind == "NAVIGATION_CHANGED"
    assert event.reason == (
        "login.submit landed on http://127.0.0.1:8000/notice, not where cp.logged_in expects"
    )


def test_a_checkpoint_whose_location_holds_is_a_checkpoint_change(run: Path) -> None:
    log = [
        {"seq": 1, "ts": _ts(1), "event": "step.start", "step": "login.submit"},
        {
            "seq": 2,
            "ts": _ts(2),
            "event": "replay.failed",
            "code": "CHECKPOINT_FAILED",
            "step": "login.submit",
            "message": "login.submit arrived somewhere, but not at cp.logged_in",
        },
        {
            "seq": 3,
            "ts": _ts(2.5),
            "event": "observe",
            "label": "login.submit.failed",
            "observation": "observations/0005.json",
        },
    ]
    [event] = read_drift(_with_screen(run, lambda o: None, log), known()).events  # type: ignore[union-attr]
    assert event.kind == "CHECKPOINT_CHANGED"
    assert event.basis == "the failure screen read against the recorded checkpoint"


# -- rates ----------------------------------------------------------------------------


def _drift(
    run_id: str,
    *,
    version: int = 3,
    tenant: str = "a",
    invocation: str | None = None,
    events: int = 0,
    injected: str | None = None,
    reached: bool = True,
    lookups: dict[str, int] | None = None,
) -> RunDrift:
    return RunDrift(
        run_id=run_id,
        invocation_id=invocation or run_id,
        capability="cap",
        version=version,
        tenant_id=tenant,
        app_family="legacy-core",
        injected=injected,
        reached_app=reached,
        lookups=lookups or {"role_name": 2, "near_text": 3},
        events=[
            DriftEvent(
                capability="cap",
                version=version,
                tenant_id=tenant,
                app_family="legacy-core",
                step_id=f"s.{i}",
                expected_locator_rung="role_name",
                observed_rungs=["role_name=0"],
                reason="r",
                evidence_ref=None,
                kind="CONTROL_RENAMED",
                fatal=True,
                run_id=run_id,
                invocation_id=invocation or run_id,
                at=T0,
                injected=injected,
                basis="rung attempts logged",
            )
            for i in range(events)
        ],
    )


def test_drift_rates_are_events_per_invocation_by_capability_tenant_and_rung() -> None:
    runs = [
        _drift("r1", events=1, injected="renamed_button"),
        _drift("r2", invocation="idem:k"),
        _drift("r3", invocation="idem:k"),  # a retry: one invocation
        _drift("r4", tenant="b", version=4),
        _drift("r5", reached=False, events=0),  # refused before a browser: not counted
    ]
    report = aggregate(runs)
    assert (report.runs, report.invocations, report.drift_events, report.rate) == (4, 3, 1, 0.3333)
    by_cap = {
        r.key: (r.drift_events, r.denominator, r.rate, r.injected) for r in report.by_capability
    }
    assert by_cap == {"cap v3": (1, 2, 0.5, 1), "cap v4": (0, 1, 0.0, 0)}
    assert {r.key: r.rate for r in report.by_tenant} == {"a": 0.5, "b": 0.0}
    by_rung = {r.key: (r.drift_events, r.denominator) for r in report.by_rung}
    assert by_rung == {"near_text": (0, 12), "role_name": (1, 8)}
    assert aggregate(runs, exclude_injected=True).drift_events == 0


def test_every_run_on_record_can_be_scanned() -> None:
    runs = list(scan([REPO_ROOT / "evidence"], known()))
    assert runs and all(r.capability for r in runs)
    kinds = {e.kind for r in runs for e in r.events}
    assert kinds <= {"CONTROL_RENAMED"} and "CONTROL_RENAMED" in kinds


# -- candidates -----------------------------------------------------------------------


def test_a_renamed_button_becomes_a_candidate_and_production_is_untouched(
    caps: Path, state: Path, run: Path
) -> None:
    before = tree_hash(caps)
    candidate, new = propose(run, capabilities_dir=caps, state_dir=state, policy=POLICY)
    assert new and tree_hash(caps) == before  # working copy, registry, ledger: unchanged

    r = candidate.record
    assert (r.name, r.version, r.base.version, r.base.status) == (NAME, 4, 3, "approved")
    assert candidate.dir == caps / "candidates" / NAME / "v4"
    assert candidate.capability.approval_state == "draft" and candidate.capability.version == 4
    assert candidate.capability.id == load(caps / f"{NAME}.json").id  # a version, not a new one
    assert r.drift[0].kind == "CONTROL_RENAMED"
    assert r.change.step_id == "search.submit"
    assert r.change.added == [
        {
            "strategy": "role_name",
            "role": "button",
            "name": "Find",
            "exact": True,
            "within": {"frame": "main"},
        }
    ]
    assert r.change.after == r.change.added + r.change.before  # recorded rungs kept, behind
    assert r.control.recorded_name == "Search" and r.control.corroborated_by == "evidence-bot"
    assert {c.id: c.result for c in r.checks} == {
        "only_locators_changed": "pass",
        "same_control": "pass",
        "recorded_rungs_kept": "pass",
        "no_secret_in_locator": "pass",
        "step_risk": "pass",
        "draft": "pass",
        "corroboration": "info",
    }
    for rel in r.evidence:
        assert (candidate.dir / rel).is_file()
    assert (
        (candidate.dir / "rationale.md")
        .read_text(encoding="utf-8")
        .startswith(f"# {NAME} v4: candidate repair of v3")
    )

    # The default is still the version that drifted; the registry does not see v4.
    registry = Registry(caps, state_dir=state)
    assert resolve(registry, NAME).record.version == 3
    assert [v.record.version for v in registry.versions(NAME)] == [3]
    assert status(candidate, {}) == "proposed"

    # The repair names Find on the failure screen, and still Search where it is Search.
    obs_path, _ = screen_of(r.drift[0])
    assert obs_path is not None
    obs = Observation.model_validate_json(obs_path.read_text(encoding="utf-8"))
    ladder = next(s for s in candidate.capability.steps if s.id == "search.submit").target
    assert ladder is not None
    found = resolve_ladder(ladder, obs)
    assert found.kind == "resolved" and found.node.name == "Find"  # type: ignore[union-attr]
    for node in obs.nodes:
        if node.name == "Find":
            object.__setattr__(node, "name", "Search")
    again = resolve_ladder(ladder, obs)
    assert again.kind == "resolved" and again.rung_index == 1  # type: ignore[union-attr]

    # Proposing the same repair again returns it; nothing new is written.
    same, new = propose(run, capabilities_dir=caps, state_dir=state, policy=POLICY)
    assert not new and same.dir == candidate.dir
    assert Candidates(caps).versions(NAME) == [4]


def test_drift_that_the_evidence_cannot_repair_is_refused(
    tmp_path: Path, caps: Path, state: Path, run: Path
) -> None:
    clean = REPO_ROOT / "evidence" / "replay-success"
    with pytest.raises(CandidateError, match="no drift stopped"):
        propose(clean, capabilities_dir=caps, state_dir=state)

    def link_instead(obs: dict[str, Any]) -> None:
        for n in obs["nodes"]:
            if n["role"] == "button" and n["name"] == "Find":
                n["role"] = "link"

    with pytest.raises(CandidateError, match=r"CONTROL_MISSING.*Re-record the capability"):
        propose(_with_screen(run, link_instead), capabilities_dir=caps, state_dir=state)
    assert not (caps / "candidates").exists()


def test_a_repair_starts_only_from_the_exact_version_that_drifted(
    caps: Path, state: Path, run: Path
) -> None:
    header = json.loads((run / "run.json").read_text(encoding="utf-8"))
    header["capability"]["content_sha256"] = "0" * 64
    (run / "run.json").write_text(json.dumps(header), encoding="utf-8")
    with pytest.raises(CandidateError, match="is not on disk"):
        propose(run, capabilities_dir=caps, state_dir=state)


def test_the_checks_fail_a_candidate_that_changes_more_than_a_ladder(
    caps: Path, state: Path, run: Path
) -> None:
    candidate, _ = propose(run, capabilities_dir=caps, state_dir=state, policy=POLICY)
    base = load(caps / "registry" / NAME / "v3.json")
    obs_path, _ = screen_of(candidate.record.drift[0])
    assert obs_path is not None
    obs = Observation.model_validate_json(obs_path.read_text(encoding="utf-8"))
    ladder = next(s for s in base.steps if s.id == "search.submit").target
    assert ladder is not None
    node = at_recorded_place(ladder, obs)
    assert node is not None
    control = Control(role="button", name="Find", frame="main", bbox=None, found_by="test")

    data = candidate.capability.model_dump(mode="json", by_alias=True)
    data["steps"][-1].update(risk="risky", approval="required")
    data["steps"].append({**data["steps"][-1], "id": "search.extra"})
    riskier = with_changes(candidate.capability, steps=data["steps"])
    checks = {
        c.id: c
        for c in static_checks(
            base,
            riskier,
            "search.submit",
            node=node,
            observation=obs,
            policy=POLICY,
            control=control,
            registered_versions=[3],
        )
    }
    assert checks["only_locators_changed"].result == "fail"
    assert checks["step_risk"].result == "attention"

    data = candidate.capability.model_dump(mode="json", by_alias=True)
    step_ = next(s for s in data["steps"] if s["id"] == "search.submit")
    step_["target"][0]["name"] = "Password"
    other = next(n for n in obs.nodes if n.name == "Member Search" and n.role != "button")
    wrong = with_changes(candidate.capability, steps=data["steps"])
    checks = {
        c.id: c
        for c in static_checks(
            base,
            wrong,
            "search.submit",
            node=other,
            observation=obs,
            policy=POLICY,
            control=control,
            registered_versions=[3, 4],
        )
    }
    assert checks["no_secret_in_locator"].result == "fail"
    assert checks["same_control"].result == "fail"
    assert checks["draft"].result == "fail"


# -- review and approval --------------------------------------------------------------


def passing(candidate: Any, *, passed: bool = True, content: str | None = None) -> Evaluation:
    return Evaluation(
        at=T0,
        artifact_hash=content or candidate.record.artifact_hash,
        suite="core",
        repetitions=1,
        runs_dir="runs",
        tasks=[],
        checks=list(candidate.record.checks),
        gates=[Gate(id="no_regression", passed=passed, detail="worse on lookup-success")],
        passed=passed,
    )


def evaluated(caps: Path, candidate: Any, **kw: Any) -> Any:
    store = Candidates(caps)
    store.write(
        candidate.dir, candidate.record.model_copy(update={"evaluation": passing(candidate, **kw)})
    )
    return store.read(candidate.dir)


def test_a_candidate_is_approved_only_after_its_evaluation_passed(
    caps: Path, state: Path, run: Path
) -> None:
    candidate, _ = propose(run, capabilities_dir=caps, state_dir=state, policy=POLICY)
    path = candidate.path
    record_review(candidate.capability, path, state_dir=state)
    with pytest.raises(ApprovalRefused, match="has not been evaluated"):
        approve(path, "reviewer", state_dir=state)

    evaluated(caps, candidate, passed=False)
    with pytest.raises(ApprovalRefused, match="failed its evaluation: worse on lookup-success"):
        approve(path, "reviewer", state_dir=state)

    evaluated(caps, candidate, content="f" * 64)
    with pytest.raises(ApprovalRefused, match="has not been evaluated"):
        approve(path, "reviewer", state_dir=state)

    working = (caps / f"{NAME}.json").read_bytes()
    ready = evaluated(caps, candidate)
    assert status(ready, {}) == "ready for review"
    approved = approve(path, "reviewer", state_dir=state)
    assert approved.version == 4 and approved.approval_state == "approved"

    registry = Registry(caps, state_dir=state)
    assert resolve(registry, NAME).record.version == 4  # the new default
    assert [(v.record.version, v.status) for v in registry.versions(NAME)] == [
        (3, "approved"),
        (4, "approved"),
    ]
    assert (caps / "registry" / NAME / "v4.json").is_file()
    assert registry.ledger()[-1].reason.startswith("candidate repair of v3")
    assert (caps / f"{NAME}.json").read_bytes() == working  # the working copy is not touched
    registered = {
        (v.record.name, v.record.version): (v.status, v.record.artifact_hash)
        for v in registry.all_versions()
    }
    assert status(Candidates(caps).read(candidate.dir), registered) == "approved"


def test_a_rejected_or_hand_edited_candidate_is_never_approved(
    caps: Path, state: Path, run: Path
) -> None:
    candidate, _ = propose(run, capabilities_dir=caps, state_dir=state, policy=POLICY)
    ready = evaluated(caps, candidate)

    text = ready.path.read_text(encoding="utf-8").replace('"Find"', '"Transfer all"')
    original = ready.path.read_text(encoding="utf-8")
    ready.path.write_text(text, encoding="utf-8")
    edited = Candidates(caps).read(ready.dir)
    assert edited.edited_outside and status(edited, {}) == "edited by hand"
    assert "changed after it was proposed" in (
        approval_refusal(ready.path, edited.capability) or ""
    )
    ready.path.write_text(original, encoding="utf-8")

    assert (
        main(
            [
                "drift",
                "reject",
                NAME,
                "--version",
                "4",
                "--by",
                "reviewer",
                "--reason",
                "the rename is not rolled out",
                "--capabilities-dir",
                str(caps),
            ]
        )
        == 0
    )
    record_review(ready.capability, ready.path, state_dir=state)
    with pytest.raises(ApprovalRefused, match="rejected by reviewer: the rename is not rolled out"):
        approve(ready.path, "reviewer", state_dir=state)
    assert status(Candidates(caps).read(ready.dir), {}) == "rejected"


# -- evaluation gates -----------------------------------------------------------------


def side(exact: int, runs: int = 1, wrong: int = 0) -> SideResult:
    return SideResult(
        runs=runs,
        exact=exact,
        safe_stop=runs - exact - wrong,
        wrong=wrong,
        outcomes=["success"] * exact + ["failure:X"] * (runs - exact),
        run_ids=[None] * runs,
    )


def task(
    tid: str, inc: SideResult, cand: SideResult, *, drift: bool = False, tags: Any = ()
) -> TaskResult:
    return TaskResult(
        task_id=tid,
        name=tid,
        tags=list(tags),
        inject="renamed_button" if drift else None,
        reproduces_drift=drift,
        incumbent=inc,
        candidate=cand,
    )


def test_the_gates_catch_a_regression_a_missed_drift_and_an_unsafe_answer(
    caps: Path, state: Path, run: Path
) -> None:
    candidate, _ = propose(run, capabilities_dir=caps, state_dir=state, policy=POLICY)

    def gates(results: list[TaskResult]) -> dict[str, bool]:
        return {g.id: g.passed for g in _gates(candidate, results)}

    good = [
        task("clean", side(1), side(1)),
        task("drift", side(0), side(1), drift=True),
        task("no-consent", side(1), side(1), tags=["security"]),
    ]
    assert all(gates(good).values())
    assert gates([])["tasks_ran"] is False
    bad = gates(
        [
            task("clean", side(1), side(0)),
            task("drift", side(0), side(0), drift=True),
            task("no-consent", side(1), side(0, wrong=1), tags=["security"]),
        ]
    )
    assert bad == {
        "checks": True,
        "tasks_ran": True,
        "repairs_the_drift": False,
        "no_regression": False,
        "no_wrong_answer": False,
        "security_tasks": False,
    }


def test_the_benchmark_tasks_for_a_capability_are_its_own_automated_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    ids = [t.id for t in tasks_for(NAME)]
    assert "lookup-success" in ids and "lookup-renamed-button" in ids
    assert "handoff-resume" not in ids and not any(i.startswith("open-") for i in ids)


# -- the CLI --------------------------------------------------------------------------


def test_the_drift_commands(
    caps: Path, run: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    roots = ["--runs-dir", str(run.parent), "--capabilities-dir", str(caps)]
    assert main(["drift", "scan", *roots]) == 0
    out = capsys.readouterr().out
    assert "search.submit  CONTROL_RENAMED, stopped the run  [injected renamed_button]" in out
    assert "cua drift propose <run id>" in out

    assert main(["drift", "scan", "--json", *roots]) == 0
    [event] = json.loads(capsys.readouterr().out)
    assert event["kind"] == "CONTROL_RENAMED"

    assert main(["drift", "report", "--json", *roots]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["drift_events"] == 1 and report["by_rung"][1]["key"] == "role_name"
    assert main(["drift", "report", *roots]) == 0
    assert "By recorded locator rung (drift events / lookups)" in capsys.readouterr().out

    assert main(["drift", "propose", str(run), "--capabilities-dir", str(caps)]) == 0
    out = capsys.readouterr().out
    assert "proposed member_savings_balance v4, a candidate repair of v3" in out
    assert '+ role_name button "Find"' in out

    assert main(["drift", "candidates", "--capabilities-dir", str(caps)]) == 0
    assert "member_savings_balance v4  proposed" in capsys.readouterr().out
    assert main(["drift", "show", NAME, "--capabilities-dir", str(caps)]) == 0
    assert "## The proposed change" in capsys.readouterr().out
    assert main(["drift", "show", "nothing", "--capabilities-dir", str(caps)]) == 64
    assert main(["drift", "propose", "run_NOPE", "--runs-dir", str(tmp_path)]) == 64
