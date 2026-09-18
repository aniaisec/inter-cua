"""What a replay returns: exactly one of four kinds, each a distinct type.

A calling agent branches on ``kind`` and never has to parse prose:

``success``           the capability did what it says; ``outputs`` are typed.
``business_outcome``  the app answered, and the answer is a declared business
                      fact (``NOT_FOUND``, ``PERMISSION_DENIED``, ...). Not an
                      error: the caller asked a question and this is the
                      answer. Carries whatever optional outputs could be read.
``failure``           the capability could not be carried out. Names the step,
                      what was expected, what was seen instead — and
                      ``side_effect``, the one field a caller must read before
                      deciding to try again.
``escalated``         a human has been asked to take over. The caller holds a
                      ``resume_token`` instead of waiting on a person.

``side_effect`` is ``none`` (nothing was committed), ``committed`` (an
irreversible step is known to have happened) or ``unknown``: an irreversible
step was performed and its outcome could not be observed. ``unknown`` is its own
state because the right response to it is neither "retry" nor "give up" but
"find out" — retrying could commit twice, giving up could leave a half-done
change nobody knows about.

The idempotency cache lives here too: the same key with the same inputs returns
the stored result instead of running again, which is what makes it safe for a
caller to retry a request whose answer it never received.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

SideEffect = Literal["none", "committed", "unknown"]

BusinessCode = str
"""Declared per capability in ``contract.outcomes``."""

FailureCode = Literal[
    "INPUT_INVALID",
    "LOCATOR_UNRESOLVED",
    "LOCATOR_AMBIGUOUS",
    "ACTION_FAILED",
    "CHECKPOINT_FAILED",
    "EXTRACTION_FAILED",
    "APP_ERROR",
    "TIMEOUT",
    "RECOVERY_EXHAUSTED",
    "POLICY_BLOCKED",
    "ESCALATION_ABORTED",
]

EscalationReason = Literal["STUCK", "NEEDS_APPROVAL", "UNRECOVERABLE", "DEAD_END"]

OutputValue: TypeAlias = str | int
"""Decimals travel as strings (``"1411.21"``): exact, and JSON has no decimal."""

EXIT_CODES: dict[str, int] = {
    "success": 0,
    "failure": 1,
    "business_outcome": 2,
    "escalated": 3,
}


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Recovery(_Model):
    """A surprise the capability knew how to handle, and handled."""

    step_id: str
    code: str
    action: str
    """What was done: ``dismiss_notice``, ``retry_step``, ``relogin``."""
    attempts: int = 1
    resumed_after_checkpoint: str | None = None
    """For a restart: the checkpoint the resume-state search found holding."""


class Handoff(_Model):
    """A stretch of the run a human was in control of (M6)."""

    request_id: str
    reason: EscalationReason
    human_actions_count: int = 0
    resumed_at: str | None = None
    resumed_after_checkpoint: str | None = None


class Evidence(_Model):
    run_dir: str | None = None
    screenshots: list[str] = Field(default_factory=list)
    """Relative to ``run_dir``; the last one is the screen the run ended on."""
    trace: str | None = None
    """Playwright trace, kept for failures."""
    intervention: str | None = None


class _Common(_Model):
    run_id: str | None = None
    capability: str
    capability_version: int
    idempotency_key: str | None = None
    duration_ms: int = 0
    locator_rungs_used: dict[str, str] = Field(default_factory=dict)
    """Step id (or ``outputs.<name>``) → the rung that named it on this run.
    Compared with the artifact's provenance, a difference is drift."""
    recoveries: list[Recovery] = Field(default_factory=list)
    handoffs: list[Handoff] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    """Things that did not stop the run and should not go unnoticed — above
    all, a locator that answered on a weaker rung than it was recorded on."""
    evidence: Evidence = Field(default_factory=Evidence)
    cached: bool = False
    """Returned from the idempotency cache: nothing was run this time."""


class Success(_Common):
    kind: Literal["success"] = "success"
    outputs: dict[str, OutputValue]
    step_id: str | None = None
    side_effect: Literal["none", "committed"] = "none"


class BusinessOutcome(_Common):
    kind: Literal["business_outcome"] = "business_outcome"
    code: BusinessCode
    payload: dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    """What the screen said, verbatim (after redaction)."""
    outputs: dict[str, OutputValue] = Field(default_factory=dict)
    """Declared optional outputs that could be read on the screen that ended
    the run — the name of the member you may not see, say."""
    step_id: str | None = None
    side_effect: SideEffect = "none"


class Failure(_Common):
    kind: Literal["failure"] = "failure"
    code: FailureCode
    step_id: str | None = None
    expected: str = ""
    observed: str = ""
    """A compact, redacted excerpt of what was on screen instead."""
    message: str = ""
    side_effect: SideEffect = "none"
    escalation_reason: EscalationReason | None = None
    """Set when this fault is one a human should have been asked about, and
    no one could be (``budget.allow_escalation`` is false, or no operator
    console is attached). The failure is returned in the escalation's place."""


class Escalated(_Common):
    kind: Literal["escalated"] = "escalated"
    reason: EscalationReason
    step_id: str | None = None
    message: str = ""
    side_effect: Literal["none", "committed"] = "none"
    request_id: str
    resume_token: str
    operator_url: str | None = None


ReplayResult: TypeAlias = Annotated[
    Success | BusinessOutcome | Failure | Escalated, Field(discriminator="kind")
]
RESULT: TypeAdapter[ReplayResult] = TypeAdapter(ReplayResult)


def exit_code(result: ReplayResult) -> int:
    return EXIT_CODES[result.kind]


FIRST = ("kind", "code", "reason", "step_id", "side_effect", "outputs", "payload", "message")
"""Printed first, so the answer is the first thing a reader sees."""


def to_json(result: ReplayResult) -> str:
    data = result.model_dump(mode="json")
    ordered = {k: data[k] for k in FIRST if k in data}
    ordered.update((k, v) for k, v in data.items() if k not in ordered)
    return json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def fingerprint(capability: str, version: int, inputs: dict[str, str]) -> str:
    """What makes two requests "the same request". Inputs are hashed, never
    stored: a sensitive input must not end up in the cache in the clear."""
    canonical = json.dumps(
        {"capability": capability, "version": version, "inputs": inputs},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyConflict(Exception):
    """The key was used before for a different request."""


class IdempotencyCache:
    """One file per key under ``<runs dir>/.idempotency/``.

    A file rather than a store because a single process is the whole
    deployment here; the interface is what a shared store would implement.
    Every kind of result is kept, failures included: the request a caller
    retries after a lost answer is exactly the one whose answer may have been
    ``side_effect: unknown``, and running it again is how a commit happens
    twice.
    """

    def __init__(self, root: Path) -> None:
        self.dir = root / ".idempotency"

    def _path(self, key: str) -> Path:
        # The key is caller-supplied; hashing it keeps it out of the path.
        return self.dir / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".json")

    def get(self, key: str, request: str) -> ReplayResult | None:
        path = self._path(key)
        if not path.is_file():
            return None
        stored = json.loads(path.read_text(encoding="utf-8"))
        if stored["fingerprint"] != request:
            raise IdempotencyConflict(
                f"idempotency key {key!r} was already used for a different request "
                "(another capability, version or inputs)"
            )
        result = RESULT.validate_python(stored["result"])
        return result.model_copy(update={"cached": True})

    def put(self, key: str, request: str, result: ReplayResult) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        record = {"fingerprint": request, "result": result.model_dump(mode="json")}
        self._path(key).write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
