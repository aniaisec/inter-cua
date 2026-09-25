"""A candidate repair for a drift, proposed from the run's own evidence.

For ``CONTROL_RENAMED`` the evidence is enough, and no model is asked: the
failure screen was kept, the version that failed is on disk, and its pixel
rung still finds exactly one control of the recorded role at the recorded
place. That control is taken to be the same one under a new name, and the
repair is a ladder that names it the way the recorder would have
(``ladder_for``): new rungs in front, the recorded rungs kept behind them.
Every other kind of drift is reported and refused: guessing which control
replaced a missing one is a discovery run's job, with a person reviewing it.

**Nothing in production changes.** The candidate is written under
``capabilities/candidates/`` as the next version number, in draft. The
working copy, the registered versions and the lifecycle ledger are not
touched; the version that failed stays approved and stays the default, until
a person approves the candidate (``cua.drift.store``).
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cua.artifact.schema import Capability
from cua.artifact.store import CAPABILITIES_DIR, save
from cua.drift.checks import static_checks
from cua.drift.detect import (
    Known,
    at_recorded_place,
    read_drift,
    recorded_name,
    recorded_role,
    screen_of,
)
from cua.drift.models import REPAIRABLE, Base, CandidateRecord, Change, Control, DriftEvent
from cua.drift.store import CAPABILITY, EVIDENCE, Candidate, CandidateError, Candidates
from cua.policy.allowlist import Policy
from cua.policy.approval import STATE_DIR
from cua.registry.models import Version
from cua.registry.store import Registry
from cua.surface.locators import BBox, Ladder, ladder_for
from cua.surface.protocol import Node, Observation


def propose(
    run_dir: Path,
    *,
    capabilities_dir: Path = CAPABILITIES_DIR,
    state_dir: Path = STATE_DIR,
    policy: Policy | None = None,
    step: str | None = None,
) -> tuple[Candidate, bool]:
    """The candidate, and whether it is new (False: the same repair was
    already proposed, and that one is returned)."""
    registry = Registry(capabilities_dir, state_dir=state_dir)
    versions = registry.all_versions()
    candidates = Candidates(capabilities_dir)
    known = Known(v.capability for v in versions)
    drift = read_drift(run_dir, known)
    if drift is None:
        raise CandidateError(f"{run_dir.as_posix()} is not a replay run of a capability")
    fatal = [e for e in drift.events if e.fatal and (step is None or e.step_id == step)]
    if not fatal:
        seen = ", ".join(f"{e.kind} at {e.step_id}" for e in drift.events) or "none"
        raise CandidateError(
            f"no drift stopped {drift.run_id}"
            + (f" at {step}" if step else "")
            + f" (drift seen: {seen}); a candidate answers drift that broke a run"
        )
    event = fatal[0]
    if event.kind not in REPAIRABLE:
        raise CandidateError(
            f"{event.step_id}: {event.kind} ({event.reason}). The evidence does not say what "
            "the step should act on instead, so no repair is proposed. Re-record the "
            "capability (`cua discover`, then `cua record`) and review it like a new version."
        )
    base = _base(versions, run_dir, event)
    obs_path, shot_path = screen_of(event)
    if obs_path is None:
        raise CandidateError(f"{drift.run_id} kept no screen from the moment of the failure")
    obs = Observation.model_validate_json(obs_path.read_text(encoding="utf-8"))
    cap = base.capability
    target = next(s for s in cap.steps if s.id == event.step_id)
    ladder: Ladder = list(target.target or [])
    node = at_recorded_place(ladder, obs)
    if node is None or node.role != recorded_role(ladder):
        raise CandidateError(
            f"{event.step_id}: the control at the recorded place is not a "
            f"{recorded_role(ladder)} on the kept screen; no repair is proposed"
        )
    added = [r for r in ladder_for(node, obs) if not isinstance(r, BBox) and r not in ladder]
    if not added:
        raise CandidateError(
            f"{event.step_id}: no rung names the {node.role} {node.name!r} uniquely on the "
            "kept screen; no repair is proposed"
        )
    after = [_dump(r) for r in [*added, *ladder]]
    for existing in candidates.all(cap.name):
        r = existing.record
        if (
            r.base.artifact_hash == base.record.artifact_hash
            and r.change.step_id == event.step_id
            and r.change.after == after
            and not existing.edited_outside
        ):
            return existing, False

    repaired = _with_ladder(cap, event.step_id, [*added, *ladder])
    number = (
        max(
            [v.record.version for v in versions if v.record.name == cap.name]
            + candidates.versions(cap.name)
            + [cap.version]
        )
        + 1
    )

    d = candidates.dir_for(cap.name, number)
    if d.exists():
        raise CandidateError(f"{d.as_posix()} already exists")
    sealed = save(
        _renumbered(repaired, number),
        d / CAPABILITY,
    )
    evidence = _keep_evidence(d, run_dir, drift.run_id, obs_path, shot_path)
    control = Control(
        role=node.role,
        name=node.name,
        frame=node.frame,
        bbox=node.bbox.model_dump() if node.bbox else None,
        found_by=(
            f"the recorded pixel rung finds exactly this {node.role}, and nothing else, "
            "where the recorded one was"
        ),
        recorded_name=recorded_name(ladder),
        corroborated_by=_corroborated(run_dir, node),
    )
    record = CandidateRecord(
        name=cap.name,
        version=number,
        capability_id=cap.id,
        artifact_hash=sealed.content_hash(),
        created_at=_now(),
        base=Base(
            version=cap.version,
            artifact_hash=base.record.artifact_hash,
            status=base.status,
            path=base.record.path,
        ),
        drift=[event],
        change=Change(
            step_id=event.step_id,
            before=[_dump(r) for r in ladder],
            after=after,
            added=[_dump(r) for r in added],
        ),
        control=control,
        evidence=evidence,
        checks=static_checks(
            cap,
            sealed,
            event.step_id,
            node=node,
            observation=obs,
            policy=policy,
            control=control,
            registered_versions=[
                v.record.version
                for v in versions
                if v.record.name == cap.name and v.record.registered
            ],
        ),
    )
    candidates.write(d, record)
    return candidates.read(d), True


def _base(versions: list[Version], run_dir: Path, event: DriftEvent) -> Version:
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    content = (run.get("capability") or {}).get("content_sha256")
    for v in versions:
        if (
            v.record.name == event.capability
            and v.record.version == event.version
            and v.record.artifact_hash == content
        ):
            return v
    raise CandidateError(
        f"{event.capability} v{event.version}, as that run ran it, is not on disk (neither "
        "registered nor the working copy); a repair must start from the exact version that "
        "drifted"
    )


def _with_ladder(cap: Capability, step_id: str, ladder: Ladder) -> Capability:
    data = cap.model_dump(mode="json", by_alias=True)
    for s in data["steps"]:
        if s["id"] == step_id:
            s["target"] = [_dump(r) for r in ladder]
    data["provenance"]["locator_rungs_used"][step_id] = ladder[0].strategy
    return Capability.model_validate(data)


def _renumbered(cap: Capability, number: int) -> Capability:
    data = cap.model_dump(mode="json", by_alias=True)
    data.update(
        version=number,
        approval_state="draft",
        approved_by=None,
        approved_at=None,
        content_sha256=None,
    )
    return Capability.model_validate(data)


def _dump(rung: Any) -> dict[str, Any]:
    out: dict[str, Any] = rung.model_dump(mode="json", exclude_none=True)
    return out


def _keep_evidence(d: Path, run_dir: Path, run_id: str, obs: Path, shot: Path | None) -> list[str]:
    """Copies of the screen the repair was built from. They come from a run
    directory, which was scrubbed of secrets when it was written."""
    out_dir = d / EVIDENCE / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    kept = []
    for src, name in (
        (obs, "failure-observation.json"),
        (shot, "failure-screenshot.png"),
        (run_dir / "result.json", "result.json"),
        (run_dir / "run.json", "run.json"),
    ):
        if src is not None and src.is_file():
            shutil.copyfile(src, out_dir / name)
            kept.append((out_dir / name).relative_to(d).as_posix())
    return kept


def _corroborated(run_dir: Path, node: Node) -> str | None:
    """Who, holding the controls during this run, clicked a control with
    this role and name. Browser actions are attributed through the console
    action (take control) of the same request."""
    path = run_dir / "human_actions.jsonl"
    if not path.is_file():
        return None
    actions = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            actions.append(json.loads(line))
        except ValueError:
            continue
    holders = {a.get("request_id"): a.get("by") for a in actions if a.get("by")}
    for action in actions:
        target = action.get("target")
        if (
            action.get("action") == "click"
            and isinstance(target, dict)
            and target.get("role") == node.role
            and target.get("name") == node.name
            and action.get("frame", node.frame) == node.frame
        ):
            return str(
                action.get("by")
                or holders.get(action.get("request_id"))
                or "a person at the operator console"
            )
    return None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
