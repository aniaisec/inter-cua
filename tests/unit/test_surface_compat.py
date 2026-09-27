"""The surface compatibility contract: what a surface can do, what a
capability needs, and that one is compared with the other before anything runs.

The acceptance line is "a capability cannot silently run against an
incompatible surface". Every place that could start one is covered: the
runner before a browser starts, the engine with the surface it was actually
handed (``test_replay.py``), a workflow before its first step, and the catalog
that offers capabilities as tools.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest

from cua import catalog
from cua.artifact import requirements
from cua.artifact.schema import Capability, DetectorMatch, Entry, RecoverAction, Rung, Step
from cua.artifact.store import (
    ArtifactError,
    declare_requirements,
    load,
    open_capability,
    parse,
    save,
    with_changes,
)
from cua.escalation.lease import ControlLease, LeasedSurface
from cua.registry.store import Registry
from cua.replay.invocation import Invocation
from cua.replay.result import Failure
from cua.replay.runner import replay
from cua.surface import SUPPORTED_SURFACES
from cua.surface.adapters import ADAPTERS, PLAYWRIGHT
from cua.surface.features import FEATURE_MEANINGS, FEATURES, SurfaceDescriptor
from cua.surface.playwright_surface import PlaywrightSurface
from cua.workflow import validator
from cua.workflow.models import load_workflow
from cua.workflow.planner import plan
from tests.unit.artifacts import GOLDEN, REPO, TENANT, policy
from tests.unit.fakes import FakeSurface

LEGACY = [
    REPO / "capabilities" / "member_savings_balance.json",
    REPO / "capabilities" / "open_subaccount.json",
]
GOLDEN_11 = GOLDEN.with_name("member_savings_balance-1.1.json")
DERIVED_GOAL1 = [
    "accessibility_tree",
    "geometry",
    "fixed_viewport",
    "frames",
    "locations",
    "document_status",
    "forms",
    "screenshots",
]


def without(*features: str) -> SurfaceDescriptor:
    return PLAYWRIGHT.model_copy(
        update={"name": "degraded", "features": PLAYWRIGHT.features - set(features)}
    )


def golden() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return data


# --------------------------------------------------------------------------
# The vocabulary and the adapters
# --------------------------------------------------------------------------


def test_every_feature_says_what_it_means() -> None:
    assert set(FEATURE_MEANINGS) == set(FEATURES)


def test_the_playwright_surface_publishes_the_registered_descriptor() -> None:
    """The registry answers before a browser exists; the class answers after.
    They must be the same answer."""
    assert PlaywrightSurface.DESCRIPTOR is ADAPTERS["web"] is ADAPTERS["legacy_web"]
    assert PLAYWRIGHT.summary() == {
        "name": "playwright",
        "version": "1",
        "targets": ["web", "legacy_web"],
        "features": list(FEATURES),
    }
    assert frozenset(ADAPTERS) == SUPPORTED_SURFACES
    assert "desktop" not in ADAPTERS


def test_the_lease_wrapper_passes_the_descriptor_through() -> None:
    inner = FakeSurface([], descriptor=without("dialogs"))
    leased = LeasedSurface(inner, lambda: ControlLease(holder="automation", since="now"))
    assert leased.descriptor is inner.descriptor


def test_missing_lists_what_is_absent_in_vocabulary_order() -> None:
    assert without("frames", "forms").missing(["forms", "frames", "geometry"]) == [
        "frames",
        "forms",
    ]
    assert PLAYWRIGHT.missing(FEATURES) == []


# --------------------------------------------------------------------------
# Deriving what a capability uses
# --------------------------------------------------------------------------


def _literals(model: Any, field: str) -> set[str]:
    return set(get_args(model.model_fields[field].annotation))


def test_every_construct_the_schema_allows_has_known_requirements() -> None:
    """A rung, condition or action the table does not know is refused at load
    (``UnknownConstruct``), so the table must know every one the schema lets
    an artifact contain."""
    strategies = {s for rung in get_args(get_args(Rung)[0]) for s in _literals(rung, "strategy")}
    kinds = {k for m in get_args(get_args(DetectorMatch)[0]) for k in _literals(m, "kind")}
    kinds |= _literals(Entry, "kind")
    actions = _literals(Step, "action") | _literals(RecoverAction, "action")
    assert strategies <= set(requirements._BY_STRATEGY)
    assert kinds <= set(requirements._BY_KIND)
    assert actions <= set(requirements._BY_ACTION)


def test_requirements_follow_from_the_content() -> None:
    data = golden()
    assert requirements.derive(data) == DERIVED_GOAL1

    step = data["steps"][0]
    # Frames: only a named frame needs them; the top document does not.
    for node in requirements._dicts(data):
        if isinstance(node.get("within"), dict):
            node["within"]["frame"] = ""
        elif isinstance(node.get("within"), str):
            node["within"] = ""
    assert "frames" not in requirements.derive(data)

    step["action"], step["key"] = "press", "Enter"
    step.pop("value", None)
    data["outcome_detectors"].append(
        {"code": "APP_ERROR", "class": "hard", "match": {"kind": "dialog_raised"}}
    )
    derived = requirements.derive(data)
    assert "keyboard" in derived and "dialogs" in derived


def test_an_unknown_construct_is_refused_not_assumed_harmless() -> None:
    with pytest.raises(requirements.UnknownConstruct, match="hover"):
        requirements.derive({"steps": [{"action": "hover"}]})


def test_screenshots_are_a_run_requirement_unless_the_run_takes_none() -> None:
    cap = parse(golden())
    assert "screenshots" in requirements.for_run(cap, handoff=False).run
    assert "screenshots" not in requirements.for_run(cap, handoff=False, screenshots=False).run
    # A person asked to help always gets the screen.
    handed = requirements.for_run(cap, handoff=True, screenshots=False).run
    assert {"screenshots", "session_handoff"} <= set(handed)
    assert requirements.for_run(cap, handoff=False).run == ["screenshots", "egress_control"]


# --------------------------------------------------------------------------
# Schema 1.2 and the 1.1 compatibility layer
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", LEGACY, ids=lambda p: p.stem)
def test_a_committed_1_1_capability_loads_unchanged_with_its_approval(path: Path) -> None:
    """Old capabilities are not reinterpreted: same bytes, same hash, still
    approved, and their requirements say they were derived."""
    loaded = open_capability(path)
    cap = loaded.capability
    assert cap.schema_version == "1.1" and cap.surface_requirements is None
    assert not loaded.edited_outside and cap.content_hash() == cap.content_sha256
    assert cap.approval_state == "approved" and Registry.for_path(path).approval_on_record(cap)
    assert cap.to_json() == path.read_text(encoding="utf-8")
    assert requirements.of(cap) == (DERIVED_GOAL1, "derived")
    assert requirements.refusal(cap) is None


def test_the_1_1_golden_still_parses_and_hashes_as_it_did() -> None:
    old = parse(json.loads(GOLDEN_11.read_text(encoding="utf-8")))
    new = parse(golden())
    assert with_changes(new, schema_version="1.1", surface_requirements=None) == old
    assert requirements.of(old) == (DERIVED_GOAL1, "derived")
    assert requirements.of(new) == (DERIVED_GOAL1, "declared")


def test_declaring_upgrades_to_1_2_and_changes_nothing_else() -> None:
    old = load(LEGACY[1])
    new = declare_requirements(old)
    assert new.schema_version == "1.2" and new.surface_requirements == DERIVED_GOAL1
    back = with_changes(new, schema_version="1.1", surface_requirements=None)
    assert back.content_hash() == old.content_hash()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"schema_version": "1.1"},
            "surface_requirements is a schema 1.2 field",
        ),
        ({"surface_requirements": None}, "declares surface_requirements"),
        (
            {"surface_requirements": ["accessibility_tree", "forms"]},
            "leaves out geometry, fixed_viewport, frames, locations, document_status, screenshots",
        ),
        ({"surface_requirements": [*DERIVED_GOAL1, "forms"]}, "lists a feature twice"),
        ({"surface_requirements": [*DERIVED_GOAL1, "telepathy"]}, "surface_requirements"),
    ],
    ids=["1.1-with-field", "1.2-without", "under-declared", "duplicate", "unknown-feature"],
)
def test_a_dishonest_or_misplaced_declaration_is_rejected(
    change: dict[str, Any], message: str
) -> None:
    with pytest.raises(ArtifactError, match=message):
        parse(golden() | change)


def test_a_declaration_may_ask_for_more_than_the_content_uses() -> None:
    cap = parse(golden() | {"surface_requirements": [*DERIVED_GOAL1, "dialogs"]})
    assert requirements.of(cap) == ([*DERIVED_GOAL1[:-1], "dialogs", "screenshots"], "declared")
    fits = requirements.check(cap, without("dialogs"), handoff=False)
    assert fits.missing == ["dialogs"] and not fits.ok


def test_the_declaration_is_part_of_the_content_hash() -> None:
    base = parse(golden())
    more = parse(golden() | {"surface_requirements": [*DERIVED_GOAL1, "dialogs"]})
    assert base.content_hash() != more.content_hash()


# --------------------------------------------------------------------------
# Refused before anything starts
# --------------------------------------------------------------------------


def test_replay_refuses_an_adapter_short_of_a_feature_before_a_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(ADAPTERS, "web", without("frames"))

    def no_browser() -> Any:
        raise AssertionError("a browser was started")

    result = replay(
        LEGACY[0],
        tenant=TENANT,
        policy=policy(),
        invocation=Invocation(inputs={"member_id": "10003"}),
        runs_dir=tmp_path / "runs",
        surface=no_browser,
    )
    assert isinstance(result, Failure)
    assert (result.code, result.side_effect) == ("SURFACE_INCOMPATIBLE", "none")
    assert "frames" in result.message and "derived" in result.message
    assert not (tmp_path / "runs").exists()


def test_replay_with_handoff_needs_a_surface_a_person_can_take_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cua.escalation.channel import HandoffSettings

    monkeypatch.setitem(ADAPTERS, "web", without("session_handoff"))

    def no_browser() -> Any:
        raise AssertionError("a browser was started")

    kwargs: dict[str, Any] = {
        "tenant": TENANT,
        "policy": policy(),
        "invocation": Invocation(inputs={"member_id": "10003"}),
        "runs_dir": tmp_path / "runs",
        "surface": no_browser,
    }
    result = replay(LEGACY[0], handoff=HandoffSettings(), **kwargs)
    assert isinstance(result, Failure) and result.code == "SURFACE_INCOMPATIBLE"
    assert "session_handoff" in result.message
    # Without --handoff the same surface is enough, so the refusal was about
    # the handoff and nothing else.
    assert requirements.refusal(load(LEGACY[0]), handoff=False) is None


def test_the_catalog_does_not_offer_what_no_adapter_can_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, _ = catalog.scan(REPO / "capabilities")
    assert entries and all(e.invocable for e in entries)
    monkeypatch.setitem(ADAPTERS, "web", without("forms"))
    entries, _ = catalog.scan(REPO / "capabilities")
    assert not any(e.invocable for e in entries)
    assert catalog.tools(entries) == []
    assert all("forms" in (e.unfit or "") for e in entries)


def test_a_workflow_is_refused_whole_when_a_step_cannot_run_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    caps = tmp_path / "capabilities"
    shutil.copytree(REPO / "capabilities", caps)
    p = plan(load_workflow(REPO / "workflows" / "open_member_subaccount.yaml"), Registry(caps))
    assert validator.refusals(p, TENANT) == []
    monkeypatch.setitem(ADAPTERS, "web", without("document_status"))
    refused = validator.refusals(p, TENANT)
    assert len(refused) == 2 and all("document_status" in r for r in refused)


def test_a_new_recording_is_saved_at_1_2_with_its_declaration(tmp_path: Path) -> None:
    from tests.unit.artifacts import record_goal1

    saved = save(record_goal1(), tmp_path / "cap.json")
    on_disk: dict[str, Any] = json.loads((tmp_path / "cap.json").read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == "1.2"
    assert on_disk["surface_requirements"] == DERIVED_GOAL1
    assert isinstance(saved, Capability) and requirements.of(saved)[1] == "declared"
