"""The capability artifact: a callable contract, not a step list.

A capability says what a calling agent supplies (``inputs``), what it gets back
(``outputs``), what can happen (``contract.outcomes``, ``outcome_detectors``),
what it commits (``contract.side_effects``, each step's ``risk``) — and then,
for the replay engine, how to do it: ordered ``steps`` whose controls are
named by locator ladders, ``checkpoints`` that prove where the run is, and the
detectors and recoverers that turn a surprising screen into a typed result.

The model is the source of truth and the JSON Schema is exported from it
(``export_json_schema``), so the two cannot disagree. Validation goes beyond
shape: an artifact that parses but could not be replayed safely is rejected
here, at load time, rather than half way through a run. In particular:

* an ``action`` outside the vocabulary;
* a ``${placeholder}`` that names no declared input or credential field — or a
  credential used anywhere but a step's typed ``value``, which is the only
  place a secret has any business being;
* two steps with the same id, or a checkpoint, detector or recoverer that
  refers to a step, checkpoint or recoverer that does not exist;
* ``retry.allowed`` on an ``irreversible`` step — replaying a commit because
  the confirmation screen was slow is how money moves twice;
* a ``bbox`` rung without ``recording_env``: pixels mean nothing outside the
  viewport they were measured in.

Conditions and locator rungs are the ones the surface layer defines, so the
artifact, the discovery loop and the replay engine speak one vocabulary.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cua.surface.conditions import (
    AllOf,
    AnyOf,
    Condition,
    DialogRaised,
    ErrorBannerPresent,
    LocationMatches,
    OutputExtracted,
    RegionPresent,
    TextPresent,
    ValidationMessagePresent,
    ValueSet,
    Visible,
)
from cua.surface.locators import BBox, NearText, RoleName, TableCell
from cua.surface.protocol import RecordingEnv

SCHEMA_VERSION: Literal["1.1"] = "1.1"
SCHEMA_PATH = Path("capabilities/schema/capability-1.1.json")

PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
_CREDENTIAL_REF = re.compile(r"^credentials\.([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)$")

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
StepId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")]
"""``screen.control``: ``login.username``, ``search.submit``."""
CheckpointId = Annotated[str, Field(pattern=r"^cp\.[a-z][a-z0-9_]*$")]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]
ValueType = Literal["string", "decimal", "integer"]

DONE = "done"
"""The pseudo-step id a checkpoint uses to mean "after every step, outputs
read" — the end of the run."""


class _Model(BaseModel):
    """Frozen and closed: a misspelt field is an error, not silently ignored.
    An artifact is reviewed and approved as written, so a field the engine
    would not read must not be able to look as if it does."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


Rung: TypeAlias = Annotated[
    RoleName | NearText | TableCell | BBox,
    Field(discriminator="strategy"),
]
Ladder: TypeAlias = Annotated[list[Rung], Field(min_length=1)]
"""Tried in order at replay; each rung must resolve to exactly one node."""


# --------------------------------------------------------------------------
# Target and contract
# --------------------------------------------------------------------------


class Entry(_Model):
    kind: Literal["location"] = "location"
    pattern: str
    """Where a run starts. ``{tenant.base_url}`` is bound at invoke time from
    ``tenants/<id>.yaml``, so the artifact names no deployment."""


class Target(_Model):
    app_family: str
    """Shared by every tenant running the same vendor product; the key a
    tenant overlay attaches to."""
    vendor: str
    version_hint: str = ""
    surface: Literal["web", "legacy_web", "desktop"] = "web"
    """Which perception adapter to load."""
    entry: Entry


class PayloadField(_Model):
    type: ValueType = "string"
    optional: bool = False


class OutcomeSpec(_Model):
    payload: dict[Name, PayloadField] = Field(default_factory=dict)


class Preconditions(_Model):
    session: Literal["fresh", "reuse_allowed"] = "fresh"


class Contract(_Model):
    """What a calling agent needs to know before it decides to invoke."""

    side_effects: Literal["none", "creates_record", "modifies_record", "moves_money"] = "none"
    idempotent: bool = True
    """Safe to run the whole capability again with the same inputs."""
    may_escalate: bool = False
    """Hands off to a human in normal operation (an approval), not only when
    something goes wrong."""
    preconditions: Preconditions = Field(default_factory=Preconditions)
    outcomes: dict[Code, OutcomeSpec] = Field(default_factory=dict)
    """Declared business outcomes and the payload each carries."""


class CredentialSpec(_Model):
    """Resolved by the runner at invoke time; never an input, never persisted,
    never logged."""

    ref: Annotated[str, Field(pattern=r"^secret://")]
    fields: Annotated[list[Name], Field(min_length=1)]


class InputSpec(_Model):
    type: ValueType = "string"
    required: bool = True
    pattern: str | None = None
    """Checked before the browser starts; a mismatch is ``INPUT_INVALID``."""
    sensitive: bool = False
    """Masked as ``***`` in every log and screenshot."""
    description: str = ""


class Extract(_Model):
    target: Ladder
    parse: Literal["text", "decimal", "currency_usd", "integer"] = "text"


class OutputSpec(_Model):
    type: ValueType = "string"
    optional: bool = False
    description: str = ""
    extract: Extract


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


class Retry(_Model):
    allowed: bool = False
    """Whether the engine may perform this step again after a timeout or a
    recoverable fault. Off unless someone decided re-doing it is harmless."""


class Step(_Model):
    id: StepId
    action: Literal["click", "type", "press", "select", "read"]
    target: Ladder | None = None
    value: str | None = None
    """What ``type`` or ``select`` enters. May be a ``${input}`` or a
    ``${credentials.<name>.<field>}`` placeholder, substituted at the moment
    of acting."""
    key: str | None = None
    """For ``press``."""
    intent: str | None = None
    """What the step is for, in words, when a reviewer wants to say more than
    ``cua describe`` derives from the step itself."""
    wait_before: Condition | None = None
    expect_after: Condition | None = None
    risk: Literal["safe", "risky", "irreversible"] = "safe"
    approval: Literal["none", "required"] = "none"
    """``required``: satisfied by an invocation approval token, else a human."""
    retry: Retry = Field(default_factory=Retry)
    side_effect_marker: Condition | None = None
    """Seen after a failure on this step → the commit happened
    (``side_effect: committed``); not seen → ``unknown``."""
    on_fail: Literal["fail", "escalate"] = "escalate"

    @model_validator(mode="after")
    def _shape_fits_action(self) -> Step:
        if self.action in ("click", "type", "select", "read") and self.target is None:
            raise ValueError(f"step {self.id}: a {self.action} step needs a target")
        if self.action in ("type", "select") and self.value is None:
            raise ValueError(f"step {self.id}: a {self.action} step needs a value")
        if self.action == "press" and not self.key:
            raise ValueError(f"step {self.id}: a press step needs a key")
        if self.risk == "irreversible" and self.retry.allowed:
            raise ValueError(
                f"step {self.id}: retry.allowed is true on an irreversible step; "
                "repeating a commit after a timeout can commit it twice"
            )
        return self


class Checkpoint(_Model):
    """A compound claim about where the run is, true right after ``after_step``.

    Also what the resume-state search walks, newest first, to find the last
    point a run is known to have reached."""

    id: CheckpointId
    after_step: str
    all_of: Annotated[list[Condition], Field(min_length=1)]


# --------------------------------------------------------------------------
# Outcome detection and recovery
# --------------------------------------------------------------------------


class StepTimedOut(_Model):
    """The step's ``expect_after`` did not hold in time. Not a screen
    condition — an event of the run — so only a detector may match on it."""

    kind: Literal["timeout"] = "timeout"


DetectorMatch: TypeAlias = Annotated[
    Visible
    | LocationMatches
    | TextPresent
    | RegionPresent
    | ErrorBannerPresent
    | ValidationMessagePresent
    | ValueSet
    | OutputExtracted
    | DialogRaised
    | AllOf
    | AnyOf
    | StepTimedOut,
    Field(discriminator="kind"),
]


class Scope(_Model):
    """Where a detector is listened for.

    ``after_step`` — only on the screen right after that step. ``after_checkpoint``
    — on every screen from the step that checkpoint follows onwards, so a
    detector for "sent back to the sign-on screen" cannot fire while the run
    is still signing on. Neither — every screen.
    """

    after_step: str | None = None
    after_checkpoint: str | None = None
    within: str | None = None
    """Frame to restrict matching to."""

    @model_validator(mode="after")
    def _one_anchor(self) -> Scope:
        if self.after_step is not None and self.after_checkpoint is not None:
            raise ValueError("a scope is after_step or after_checkpoint, not both")
        return self


class Recover(_Model):
    run: Name | None = None
    """Recoverer to run first."""
    then: Literal["continue", "retry_step", "restart_from_last_checkpoint"] = "continue"
    max: Annotated[int, Field(ge=1)] = 1
    backoff_s: Annotated[float, Field(ge=0)] = 0.0


class Detector(_Model):
    """Turns a screen into a typed result.

    Evaluated in fixed precedence by class — hard, then business, then
    recoverable — before the step's own ``expect_after`` and the checkpoint.
    ``class`` maps one-to-one onto the replay result kinds: hard → Failure,
    business → BusinessOutcome, recoverable → handled, and the run goes on.
    """

    code: Code
    class_: Literal["hard", "business", "recoverable"] = Field(alias="class")
    scope: Scope | None = None
    match: DetectorMatch
    payload_from: Literal["validation_message"] | None = None
    recover: Recover | None = None

    @model_validator(mode="after")
    def _recover_iff_recoverable(self) -> Detector:
        if self.class_ == "recoverable" and self.recover is None:
            raise ValueError(f"detector {self.code}: a recoverable detector needs recover")
        if self.class_ != "recoverable" and self.recover is not None:
            raise ValueError(f"detector {self.code}: only a recoverable detector may recover")
        if isinstance(self.match, StepTimedOut) and self.class_ != "recoverable":
            raise ValueError(f"detector {self.code}: a timeout is matched only to recover from")
        return self


class RecoveryLimits(_Model):
    per_step: Annotated[int, Field(ge=0)] = 1
    per_run: Annotated[int, Field(ge=0)] = 3
    """Exceeded → Failure ``RECOVERY_EXHAUSTED``."""


class RecoverAction(_Model):
    action: Literal["click", "press"]
    target: Ladder | None = None
    key: str | None = None


class Recoverer(_Model):
    """Either a few actions of its own, or a replay of steps the capability
    already has (signing on again is the capability's own sign-on steps)."""

    steps: list[RecoverAction] = Field(default_factory=list)
    sub_flow: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one(self) -> Recoverer:
        if bool(self.steps) == bool(self.sub_flow):
            raise ValueError("a recoverer has either steps or a sub_flow")
        return self


class Redaction(_Model):
    screenshot_masks: list[Ladder] = Field(default_factory=list)
    """Painted out before a screenshot exists. Input and credential
    sensitivity is declared on the fields themselves."""


class Provenance(_Model):
    """Proves the discovery run happened without carrying its transcript."""

    discovery_run_id: str
    provider: str
    model: str
    """The model that actually answered, as the provider reported it."""
    transcript_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    """Of the run's ``log.jsonl``, which stays in evidence."""
    recorded_at: str
    locator_rungs_used: dict[str, str] = Field(default_factory=dict)
    """Step id (or ``outputs.<name>``) → the rung that named it at discovery.
    Replay reports the same map; a difference is drift."""


# --------------------------------------------------------------------------
# The capability
# --------------------------------------------------------------------------

APPROVAL_FIELDS: frozenset[str] = frozenset(
    {"version", "approval_state", "approved_by", "approved_at", "content_sha256"}
)
"""Bookkeeping about the content, excluded from the content hash: approving a
capability, or numbering it, does not change what it does."""


class Capability(_Model):
    schema_version: Literal["1.1"] = SCHEMA_VERSION
    id: Annotated[str, Field(pattern=r"^cap_[0-9A-HJKMNP-TV-Z]{26}$")]
    name: Name
    version: Annotated[int, Field(ge=1)] = 1
    """Bumped on any change to what the capability does; a bump resets
    approval to draft."""
    approval_state: Literal["draft", "approved"] = "draft"
    """Unattended replay requires ``approved``."""
    approved_by: str | None = None
    approved_at: str | None = None
    content_sha256: str | None = None
    """Seal written on save. A file whose content no longer matches its seal
    was edited outside ``cua`` and is loaded as a new, unapproved version.
    Detects unreviewed edits; it is not a signature."""
    description: str

    target: Target
    contract: Contract = Field(default_factory=Contract)
    credentials: dict[Name, CredentialSpec] = Field(default_factory=dict)
    inputs: dict[Name, InputSpec] = Field(default_factory=dict)
    outputs: dict[Name, OutputSpec] = Field(default_factory=dict)
    recording_env: RecordingEnv | None = None
    steps: Annotated[list[Step], Field(min_length=1)]
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    outcome_detectors: list[Detector] = Field(default_factory=list)
    recovery_limits: RecoveryLimits = Field(default_factory=RecoveryLimits)
    recoverers: dict[Name, Recoverer] = Field(default_factory=dict)
    redaction: Redaction = Field(default_factory=Redaction)
    provenance: Provenance

    # -- validation ----------------------------------------------------------

    @model_validator(mode="after")
    def _consistent(self) -> Capability:
        problems = [
            *self._duplicate_ids(),
            *self._dangling_references(),
            *self._unresolved_placeholders(),
            *self._unanchored_pixels(),
            *self._approval_fields(),
        ]
        if problems:
            raise ValueError("\n".join(problems))
        return self

    def _duplicate_ids(self) -> Iterator[str]:
        seen: set[str] = set()
        for step_id in [s.id for s in self.steps] + [c.id for c in self.checkpoints]:
            if step_id in seen:
                yield f"duplicate id {step_id!r}"
            seen.add(step_id)

    def _dangling_references(self) -> Iterator[str]:
        steps = {s.id for s in self.steps}
        checkpoints = {c.id for c in self.checkpoints}
        for cp in self.checkpoints:
            if cp.after_step not in steps | {DONE}:
                yield f"checkpoint {cp.id} is after unknown step {cp.after_step!r}"
            for cond in _walk_conditions(cp.all_of):
                if isinstance(cond, OutputExtracted) and cond.name not in self.outputs:
                    yield f"checkpoint {cp.id} waits for undeclared output {cond.name!r}"
        for det in self.outcome_detectors:
            scope = det.scope
            if scope and scope.after_step and scope.after_step not in steps:
                yield f"detector {det.code} is scoped to unknown step {scope.after_step!r}"
            if scope and scope.after_checkpoint and scope.after_checkpoint not in checkpoints:
                yield (
                    f"detector {det.code} is scoped to unknown checkpoint "
                    f"{scope.after_checkpoint!r}"
                )
            if det.recover and det.recover.run and det.recover.run not in self.recoverers:
                yield f"detector {det.code} runs unknown recoverer {det.recover.run!r}"
            if det.class_ == "business" and det.code not in self.contract.outcomes:
                yield f"business detector {det.code} is not a declared contract outcome"
        for name, rec in self.recoverers.items():
            for step_id in rec.sub_flow:
                if step_id not in steps:
                    yield f"recoverer {name} replays unknown step {step_id!r}"

    def _unresolved_placeholders(self) -> Iterator[str]:
        inputs = set(self.inputs)
        fields = {(n, f) for n, c in self.credentials.items() for f in c.fields}
        data = self.model_dump(mode="json", by_alias=True, exclude={"provenance", "description"})
        for path, text in _strings(data):
            for match in PLACEHOLDER.finditer(text):
                name = match.group(1)
                cred = _CREDENTIAL_REF.match(name)
                if cred is None:
                    if name not in inputs:
                        yield f"{path}: ${{{name}}} is not a declared input"
                    continue
                if not _is_step_value(path):
                    yield f"{path}: a credential may only be typed as a step value"
                elif (cred.group(1), cred.group(2)) not in fields:
                    yield f"{path}: ${{{name}}} is not a declared credential field"

    def _unanchored_pixels(self) -> Iterator[str]:
        if self.recording_env is not None:
            return
        data = self.model_dump(mode="json", by_alias=True)
        for path, node in _dicts(data):
            if node.get("strategy") == "bbox":
                yield f"{path}: a bbox rung needs recording_env; pixels are not portable"

    def _approval_fields(self) -> Iterator[str]:
        if self.approval_state == "approved" and not self.approved_by:
            yield "an approved capability names who approved it (approved_by)"
        if self.approval_state == "draft" and self.approved_by:
            yield "a draft capability has no approved_by"

    # -- identity ------------------------------------------------------------

    def content_hash(self) -> str:
        """SHA-256 of everything the capability *does*: all of it except the
        numbering and approval bookkeeping."""
        data = self.model_dump(mode="json", by_alias=True, exclude=set(APPROVAL_FIELDS))
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        data = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------
# JSON Schema
# --------------------------------------------------------------------------


def json_schema() -> dict[str, Any]:
    schema = Capability.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://github.com/aniaisec/inter-cua/capability-{SCHEMA_VERSION}.json"
    return schema


def export_json_schema(path: Path = SCHEMA_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_schema(), indent=2) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _walk_conditions(conditions: list[Condition]) -> Iterator[Condition]:
    for cond in conditions:
        yield cond
        if isinstance(cond, AllOf):
            yield from _walk_conditions(cond.all_of)
        elif isinstance(cond, AnyOf):
            yield from _walk_conditions(cond.any_of)


def _strings(data: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(data, str):
        yield path, data
    elif isinstance(data, dict):
        for key, value in data.items():
            yield from _strings(value, f"{path}.{key}" if path else str(key))
    elif isinstance(data, list):
        for i, value in enumerate(data):
            yield from _strings(value, f"{path}.{i}")


def _dicts(data: Any, path: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(data, dict):
        yield path, data
        for key, value in data.items():
            yield from _dicts(value, f"{path}.{key}" if path else str(key))
    elif isinstance(data, list):
        for i, value in enumerate(data):
            yield from _dicts(value, f"{path}.{i}")


_STEP_VALUE = re.compile(r"^steps\.\d+\.value$")


def _is_step_value(path: str) -> bool:
    return _STEP_VALUE.match(path) is not None


if __name__ == "__main__":  # pragma: no cover
    print(export_json_schema())
