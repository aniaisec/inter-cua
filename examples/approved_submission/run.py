"""Execute this directory's machine-readable scenario through ordinary entry points."""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any

from examples._session import Session
from examples._support import require


def run(s: Session, stack: ExitStack) -> dict[str, Any]:
    inputs = s.spec["inputs"]
    before = s.oracle()
    denied = s.replay("adjust_stock", inputs, expected=1)
    require(denied.get("escalation_reason") == "NEEDS_APPROVAL", "write did not require consent")
    token = s.token("adjust_stock", inputs)
    wrong = s.cli(
        "replay",
        "adjust_stock",
        *s.inputs(s.spec["wrong_inputs"]),
        "--approval-token",
        token,
        expected=1,
        secret=token,
    )
    require(
        wrong.get("code") == "POLICY_BLOCKED" and s.oracle() == before,
        "consent for other inputs permitted a write",
    )
    args = [
        "replay",
        "adjust_stock",
        *s.inputs(inputs),
        "--approval-token",
        token,
        "--idempotency-key",
        "example-write",
    ]
    done = s.cli(*args, secret=token)
    s.no_models(done)
    after = s.oracle()
    require(
        done["outputs"]["quantity"]
        == before["quantities"][inputs["item_code"]] + int(inputs["quantity_change"]),
        "wrong stock quantity",
    )
    require(
        after["commit_count"] == before["commit_count"] + 1
        and after["commit_posts"] == before["commit_posts"] + 1,
        "expected exactly one commit",
    )
    retry = s.cli(*args, secret=token)
    require(
        retry["cached"] and retry["run_id"] == done["run_id"] and s.oracle() == after,
        "same-key retry repeated the write",
    )
    return {
        "kind": done["kind"],
        "side_effect": done["side_effect"],
        "commits": 1,
        "wrong_input_consent_refused": True,
        "retry_cached": True,
        "replay_model_calls": 0,
    }
