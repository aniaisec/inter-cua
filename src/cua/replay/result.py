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
from typing import Annotated, Any, Literal, TypeAlias, get_args

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
    "AUTH_FAILED",
    "TIMEOUT",
    "RECOVERY_EXHAUSTED",
    "POLICY_BLOCKED",
    "ESCALATION_ABORTED",
    "INTERRUPTED",
    "SURFACE_INCOMPATIBLE",
]
FAILURE_CODES: frozenset[str] = frozenset(get_args(FailureCode))
"""``AUTH_FAILED`` is the one a hard detector may carry as its own code: the
sign-on was refused, which is not the application failing (``APP_ERROR``) and
is the way a capability most often dies in production — a rotated password.
``INTERRUPTED`` is the run being killed from outside (Ctrl+C, a crash) and is
written so that a run cut short still says how far it got and what it may
have committed.
``SURFACE_INCOMPATIBLE`` is the capability needing a surface feature the
surface it would run on does not have (``cua.artifact.requirements``): refused
before the first action, so its side effect is always ``none``. Not retryable
on the same build; it needs another adapter, or the capability re-recorded."""

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
    outcome: Literal["succeeded", "failed"] = "succeeded"
    """Recorded as ``failed`` before the recovery runs and flipped when it is
    through, so a run that dies inside a relogin still shows the relogin."""
    resumed_after_checkpoint: str | None = None
    """For a restart: the checkpoint the resume-state search found holding."""


class Handoff(_Model):
    """One request to a human, and how it ended."""

    request_id: str
    reason: EscalationReason
    step_id: str | None = None
    """Where the run was when it asked."""
    decision: Literal["hand_back", "retry_step", "approve", "abort", "unanswered"] | None = None
    decided_by: str | None = None
    human_actions_count: int = 0
    """Things the person did in the browser (clicks, typing, key presses)."""
    resumed_at: str | None = None
    """The step the automation carried on with; ``done`` when only the outputs
    were left; None when it did not carry on."""
    resumed_after_checkpoint: str | None = None
    """The checkpoint the resume-state search found holding."""


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
    outputs: dict[str, OutputValue] = Field(default_factory=dict)
    """What was read before the failure. Filled when an irreversible step had
    landed and its confirmation screen was seen: a caller told ``committed``
    is also told the reference number, even though the run then went wrong."""
    during_recovery: str | None = None
    """``<code> at <step>`` when the failure happened inside a recovery — a
    relogin that could not find the sign-on form is reported as that, not as
    a sign-on step failing out of nowhere."""
    escalation_reason: EscalationReason | None = None
    """Set when this fault is one a person should have been asked about.
    With no handoff channel attached, the fault is returned as is, under its
    own code. With one, ``ESCALATION_ABORTED`` says the escalation itself was
    ended: ``budget.allow_escalation`` is false (nobody was asked), the
    operator aborted, or the request expired; the fault underneath is named
    in ``message``."""


class Escalated(_Common):
    kind: Literal["escalated"] = "escalated"
    reason: EscalationReason
    step_id: str | None = None
    message: str = ""
    side_effect: SideEffect = "none"
    """``unknown`` too: a commit that may have happened is exactly what a
    person is asked to go and look at."""
    request_id: str
    resume_token: str
    """Hand it to ``cua resume`` once the person hands back."""
    operator_url: str | None = None
    outputs: dict[str, OutputValue] = Field(default_factory=dict)
    """Anything already read — above all a reference number captured the
    moment a commit landed."""


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
    """One file per tenant and key under ``<runs dir>/.idempotency/``.

    A file rather than a store because a single process is the whole
    deployment here; the interface is what a shared store would implement.
    Every kind of result is kept, failures included: the request a caller
    retries after a lost answer is exactly the one whose answer may have been
    ``side_effect: unknown``, and running it again is how a commit happens
    twice.

    Keys are the caller's, and two tenants' callers may choose the same one:
    a record belongs to the tenant and the key together, so one tenant's
    result is never the answer to another's request. A record kept before
    that (with no tenant in it) is read as its run's tenant's; see
    ``_legacy``.
    """

    def __init__(self, root: Path, tenant: str) -> None:
        self.root = root
        self.dir = root / ".idempotency"
        self.tenant = tenant

    def _path(self, key: str) -> Path:
        # The key is caller-supplied; hashing it keeps it out of the path.
        return self.dir / (_digest(f"{self.tenant}\n{key}") + ".json")

    def get(self, key: str, request: str) -> ReplayResult | None:
        path = self._path(key)
        stored = (
            json.loads(path.read_text(encoding="utf-8")) if path.is_file() else self._legacy(key)
        )
        if stored is None:
            return None
        if stored["fingerprint"] != request:
            raise IdempotencyConflict(
                f"idempotency key {key!r} was already used for a different request "
                "(another capability, version or inputs)"
            )
        result = RESULT.validate_python(stored["result"])
        return result.model_copy(update={"cached": True})

    def put(self, key: str, request: str, result: ReplayResult) -> None:
        if result.kind == "escalated":
            # Not an answer yet. A caller retrying the same key would be handed
            # a resume token for a run that may since have finished or been
            # aborted; it has to reach the run's current state instead.
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        record = {
            "tenant": self.tenant,
            "fingerprint": request,
            "result": result.model_dump(mode="json"),
        }
        self._path(key).write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
        )

    def _legacy(self, key: str) -> dict[str, Any] | None:
        """A record kept under the key alone, before records were per tenant.

        It is this tenant's if the run it answered ran on this tenant. One
        refused before any run started has no run directory and certainly no
        side effect, so running the request again is safe: it is no answer.
        One with a side effect whose tenant cannot be told is a conflict:
        either answer might be another tenant's, or a second commit."""
        path = self.dir / (_digest(key) + ".json")
        if not path.is_file():
            return None
        stored: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        result = stored.get("result") or {}
        run_id = result.get("run_id")
        owner = run_tenant(self.root / run_id) if isinstance(run_id, str) else None
        if owner == self.tenant:
            return stored
        if owner is not None or result.get("side_effect", "none") == "none":
            return None
        raise IdempotencyConflict(
            f"idempotency key {key!r} answered a request before results were kept per tenant, "
            "and which tenant that was cannot be told; use a new key"
        )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def run_tenant(run_dir: Path) -> str | None:
    """The tenant a run directory's ``run.json`` says it ran on."""
    try:
        data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tenant = data.get("tenant") if isinstance(data, dict) else None
    owner = tenant.get("id") if isinstance(tenant, dict) else None
    return owner if isinstance(owner, str) else None
