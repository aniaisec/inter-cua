"""What must be true of a candidate repair before a person is asked to
approve it, checked offline against the version it repairs and the screen it
was built from.

A repair is proposed from what the app showed, and what the app shows is
not trusted: a page could rename a harmless button to something that lures a
repair onto the wrong control. So the checks hold the candidate to the
narrowest change that explains the drift:

``only_locators_changed``  nothing differs from the version it repairs except
                           the repaired step's ladder: no step added, removed
                           or reordered; no risk, approval, input, output,
                           credential, entry, detector or recoverer changed.
``same_control``           on the failure screen the new ladder names exactly
                           one control, by a rung trusted to act on, and it is
                           the control at the recorded place, with the
                           recorded role, in the recorded frame.
``recorded_rungs_kept``    the recorded rungs are all still there, behind the
                           new ones: a tenant still on the old screen is still
                           served, and the repair can be judged as an addition.
``no_secret_in_locator``   no new rung contains text the policy scrubs, or a
                           label the policy treats as sensitive.
``step_risk``              a step that commits or needs consent is flagged for
                           the reviewer's closest look (``attention``).
``draft``                  the candidate is a draft, and no version of that
                           number is registered.
``corroboration``          (``info``) whether a person, handed that same run,
                           acted on a control with this role and name.
"""

from __future__ import annotations

import re
from typing import Any

from cua.artifact.schema import Capability
from cua.drift.models import Check, Control
from cua.policy.allowlist import Policy
from cua.policy.redaction import Redactor
from cua.surface.locators import BBox, Ladder, Resolved, resolve_ladder
from cua.surface.protocol import Node, Observation

APPROVAL_FIELDS = ("version", "approval_state", "approved_by", "approved_at", "content_sha256")


def static_checks(
    base: Capability,
    candidate: Capability,
    step_id: str,
    *,
    node: Node,
    observation: Observation,
    policy: Policy | None,
    control: Control,
    registered_versions: list[int],
) -> list[Check]:
    before = _ladder(base, step_id)
    after = _ladder(candidate, step_id)
    added = after[: max(0, len(after) - len(before))]
    return [
        _only_locators_changed(base, candidate, step_id),
        _same_control(after, len(added), node, observation, candidate, before),
        _recorded_rungs_kept(before, after),
        _no_secret(added, policy),
        _step_risk(candidate, step_id),
        _draft(candidate, registered_versions),
        Check(
            id="corroboration",
            result="info",
            detail=(
                f"{control.corroborated_by} acted on a {control.role} named {control.name!r} "
                "when that run was handed over"
                if control.corroborated_by
                else "no person acted on this control during the run it was built from"
            ),
        ),
    ]


def _ladder(cap: Capability, step_id: str) -> Ladder:
    step = next(s for s in cap.steps if s.id == step_id)
    return list(step.target or [])


def _only_locators_changed(base: Capability, cand: Capability, step_id: str) -> Check:
    def strip(cap: Capability) -> dict[str, Any]:
        data = cap.model_dump(mode="json", by_alias=True)
        for f in APPROVAL_FIELDS:
            data.pop(f, None)
        for step in data["steps"]:
            if step["id"] == step_id:
                step.pop("target", None)
        data["provenance"]["locator_rungs_used"].pop(step_id, None)
        return data

    a, b = strip(base), strip(cand)
    if a == b:
        return Check(
            id="only_locators_changed",
            result="pass",
            detail=f"only {step_id}'s ladder differs from v{base.version}",
        )
    changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    return Check(
        id="only_locators_changed",
        result="fail",
        detail=f"it also changes {', '.join(changed)}; a drift repair changes a ladder only",
    )


def _same_control(
    ladder: Ladder,
    added: int,
    node: Node,
    obs: Observation,
    cand: Capability,
    before: Ladder,
) -> Check:
    outcome = resolve_ladder(ladder, obs, recording_env=cand.recording_env)
    want_role = next((getattr(r, "role", None) for r in before if getattr(r, "role", None)), None)
    frame = next((r.within.frame for r in before if r.within and r.within.frame), None)
    problems = []
    if not isinstance(outcome, Resolved):
        problems.append(f"it does not name one control on the failure screen ({outcome.kind})")
    else:
        if outcome.node.ref != node.ref:
            problems.append("it names a different control from the one at the recorded place")
        if outcome.rung_index >= added or isinstance(ladder[outcome.rung_index], BBox):
            problems.append("only the pixel rung finds it, which is not trusted to act on")
    if want_role is not None and node.role != want_role:
        problems.append(f"the control is a {node.role}; a {want_role} was recorded")
    if frame is not None and node.frame != frame:
        problems.append(f"the control is in the {node.frame!r} frame, not {frame!r}")
    if problems:
        return Check(id="same_control", result="fail", detail="; ".join(problems))
    return Check(
        id="same_control",
        result="pass",
        detail=(
            f"on the failure screen it names exactly one control, the {node.role} "
            f"{node.name!r} at the recorded place, by {ladder[0].strategy}"
        ),
    )


def _recorded_rungs_kept(before: Ladder, after: Ladder) -> Check:
    tail = after[len(after) - len(before) :] if len(after) >= len(before) else []
    if tail == before:
        return Check(
            id="recorded_rungs_kept",
            result="pass",
            detail=f"the {len(before)} recorded rung(s) follow the new one(s), unchanged",
        )
    return Check(
        id="recorded_rungs_kept",
        result="attention",
        detail="the recorded rungs were changed or dropped; the old screen may not be served",
    )


def _no_secret(added: Ladder, policy: Policy | None) -> Check:
    redactor = Redactor.for_policy(policy) if policy is not None else Redactor()
    sensitive = policy.sensitive_labels if policy is not None else r"(?i)password"
    label = re.compile(sensitive)
    bad = []
    for rung in added:
        for text in _texts(rung):
            if redactor.text(text) != text or label.search(text):
                bad.append(f"{rung.strategy} {text!r}")
    if bad:
        return Check(
            id="no_secret_in_locator",
            result="fail",
            detail="new rung text the policy treats as sensitive: " + ", ".join(bad),
        )
    return Check(
        id="no_secret_in_locator",
        result="pass",
        detail="no new rung contains text the policy scrubs or a sensitive label",
    )


def _texts(rung: Any) -> list[str]:
    return [
        str(getattr(rung, f))
        for f in ("name", "text", "row_contains", "column_header")
        if getattr(rung, f, None)
    ]


def _step_risk(cand: Capability, step_id: str) -> Check:
    step = next(s for s in cand.steps if s.id == step_id)
    if step.risk == "safe" and step.approval == "none":
        return Check(
            id="step_risk", result="pass", detail=f"{step_id} is a safe step with no consent"
        )
    return Check(
        id="step_risk",
        result="attention",
        detail=(
            f"{step_id} is {step.risk}"
            + (" and needs consent" if step.approval == "required" else "")
            + ": look at the failure screenshot and make sure the new name is the control "
            "you mean to commit with"
        ),
    )


def _draft(cand: Capability, registered: list[int]) -> Check:
    if cand.approval_state == "draft" and cand.version not in registered:
        return Check(
            id="draft",
            result="pass",
            detail=f"v{cand.version} is a draft and no v{cand.version} is registered",
        )
    return Check(
        id="draft",
        result="fail",
        detail=f"v{cand.version} is {cand.approval_state}, or a v{cand.version} is registered",
    )
