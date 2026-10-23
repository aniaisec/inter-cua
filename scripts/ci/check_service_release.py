"""Verify PR17 recovery evidence; readiness still requires operator review."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from pydantic import Field

from scripts.ci.check_adoption_release import Evidence, Gate, Record, verify_evidence
from scripts.ci.check_test_results import check as check_tests

# Select named regressions so an unrelated passing report cannot satisfy a gate.
EXERCISES = {
    "concurrency": (
        "tests/unit/test_invocation_ownership.py::"
        "test_concurrent_requests_and_completed_retry_never_start_another_session",
        "tests/unit/test_invocation_ownership.py::"
        "test_same_token_competitors_can_dispatch_only_one_commit",
    ),
    "saturation": (
        "tests/unit/test_admission.py::test_capacity_has_no_phantom_claims_and_retries_still_resolve",
        "tests/unit/test_admission.py::test_concurrent_overflow_is_bounded_before_persistence",
        "tests/unit/test_admission.py::test_mcp_dispatch_is_bounded_while_status_and_ping_stay_responsive",
    ),
    "operator_isolation": (
        "tests/unit/test_operator_access.py::test_every_surface_requires_verified_identity",
        "tests/unit/test_operator_access.py::test_roles_tenant_grants_and_spoofed_names",
        "tests/unit/test_operator_access.py::test_assignment_survives_restart_and_blocks_other_operators",
        "tests/unit/test_operator_access.py::test_competing_processes_have_one_durable_assignee",
    ),
    "expired_handoffs": (
        "tests/unit/test_operator_access.py::test_expired_request_cannot_be_claimed_or_approved",
    ),
    "process_loss": (
        "tests/unit/test_invocation_ownership.py::test_killed_process_never_repeats_a_write",
        "tests/unit/test_invocation_ownership.py::"
        "test_interruption_during_resume_does_not_reuse_a_stale_escalation",
        "tests/unit/test_admission.py::test_restarted_queue_returns_an_interruption_without_dispatch",
    ),
    "shutdown": (
        "tests/unit/test_admission.py::test_shutdown_cancels_waiting_work_and_finishes_the_active_run",
        "tests/unit/test_admission.py::test_queue_expiry_never_dispatches_or_grows_a_cancelled_backlog",
    ),
    "backup_restore": (
        "tests/unit/test_service_recovery.py::test_quiescent_restore_preserves_answers_and_spent_consent",
    ),
    "retention": (
        "tests/unit/test_maintenance.py::"
        "test_cleanup_is_scoped_dry_by_default_and_preserves_retry_authority",
        "tests/unit/test_maintenance.py::test_protected_evidence_is_preserved",
    ),
}
MANUAL_GATES = (
    "pr13_envelope_recovery",
    "pr15_session_cleanup",
    "tls_supervisor_restart",
    "target_application_reconciliation",
    "deployment_backup_restore",
)
BLOCKERS = [
    "PR13 workflow/service envelope migration and process-loss qualification remain incomplete.",
    "PR15 verified browser termination and abandoned-profile cleanup remain incomplete.",
]
VARIANTS = {
    "test_killed_process_never_repeats_a_write": ("reserved", "dispatched", "committed", "result"),
    "test_queue_expiry_never_dispatches_or_grows_a_cancelled_backlog": ("True", "False"),
    "test_protected_evidence_is_preserved": ("unknown", "handoff", "active", "recent"),
    "test_every_surface_requires_verified_identity": (
        "/",
        "/requests/req_x",
        "/requests/req_x/screenshot",
        "/api/requests",
        "/api/requests/req_x",
        "/api/requests/req_x/history",
        "/api/requests/req_x/session",
        "/health",
        "/ready",
        "/docs",
        "/openapi.json",
    ),
}


class ExerciseRecord(Record):
    schema_version: Literal[1]
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_dirty: bool
    environment: str = Field(min_length=1)
    exercises: dict[str, Evidence]
    manual_gates: dict[str, Gate]
    open_blockers: list[str]


def check_exercise(name: str, report: Path) -> dict[str, object]:
    summary = check_tests(report)
    cases = list(ET.parse(report).getroot().iter("testcase"))
    for selector in EXERCISES[name]:
        module, function = selector.split("::")
        classname = module.removesuffix(".py").replace("/", ".")
        expected = {f"{function}[{variant}]" for variant in VARIANTS.get(function, ())}
        expected = expected or {function}
        found = {case.get("name") for case in cases if case.get("classname") == classname}
        if not expected.issubset(found):
            raise ValueError(f"{name} report is missing required test: {selector}")
    return summary


def check(path: Path, *, commit: str) -> dict[str, object]:
    record = ExerciseRecord.model_validate_json(path.read_text(encoding="utf-8"))
    if record.commit != commit or record.source_dirty:
        raise ValueError("release evidence must match the reviewed commit from a clean checkout")
    if set(record.exercises) != set(EXERCISES):
        raise ValueError("record must contain every recovery exercise and no unknown exercises")
    summaries = {}
    for name, evidence in record.exercises.items():
        verify_evidence(evidence, path.parent)
        summaries[name] = check_exercise(name, path.parent / evidence.path)
    if set(record.manual_gates) != set(MANUAL_GATES):
        raise ValueError("record must contain every manual release gate and no unknown gates")
    for name, gate in record.manual_gates.items():
        if gate.status != "passed" or gate.evidence is None:
            raise ValueError(f"manual gate has no passing reviewed evidence: {name}")
        verify_evidence(gate.evidence, path.parent)
    if record.open_blockers:
        raise ValueError("shared-service release has unresolved blockers")
    return {"status": "ready_for_review", "commit": commit, "exercises": summaries}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("--commit", required=True, help="Full reviewed candidate commit")
    args = parser.parse_args()
    try:
        result = check(args.record, commit=args.commit)
    except (OSError, ValueError, ET.ParseError) as exc:
        parser.exit(1, f"service release not ready: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
