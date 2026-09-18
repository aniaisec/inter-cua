"""A finished discovery run → a draft capability.

The recorder reads only what the run directory holds — ``run.json``,
``log.jsonl``, ``model_calls.jsonl`` and the stored observations — and never
the model's prose. The artifact must stand on its own for review, so what the
agent *did* is recorded and what it *said* stays in evidence.

What it derives, and from what:

* **steps** from ``step`` events, in order. Each names its control by the
  ladder the loop built when the agent acted (re-derived from the stored
  observation if absent). ``read`` calls are left out: they are how the agent
  looked, and outputs are extracted by the ladders ``done`` named.
* **templating**: a typed value becomes ``${param}`` only when it *equals* the
  value a declared ``--param`` was given. Credentials already arrive as
  ``${credentials.<name>.<field>}``. Nothing else is templated — a literal that
  happens to contain a member id is left alone rather than guessed at — and a
  step that typed a masked value is refused, because it means a secret was
  typed literally and the artifact could not replay it.
* **risk** from the same policy check the loop ran: a step the policy says
  needs approval is ``irreversible``, ``approval: required``, never retried.
* **checkpoints** after every step that changed the screen: the location of
  the frame that changed, its heading, and any bound input value shown on it
  (``text_present: ${member_id}``). The last one also names the heading above
  each output, which is what tells a member's detail page apart from the
  "not authorized" page that shares its title.
* **outputs** from ``agent.done``: the node each ref named on the final
  screen, re-described by position (``ladder_for(..., for_value=True)``) so
  the ladder never names the value it read.
* **detectors, recoverers, masks** from the app-family template
  (``capabilities/families/<app_family>.yaml``) — what the product does when
  things go wrong is not something one happy-path run can learn.
* **provenance**: run id, the model that actually answered (from
  ``model_calls.jsonl``, not the alias that was asked for), and the SHA-256
  of ``log.jsonl``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from ulid import ULID

from cua.artifact.schema import (
    DONE,
    PLACEHOLDER,
    Capability,
    Checkpoint,
    Contract,
    CredentialSpec,
    Detector,
    Entry,
    Extract,
    InputSpec,
    OutcomeSpec,
    OutputSpec,
    Provenance,
    Recoverer,
    RecoveryLimits,
    Redaction,
    Retry,
    Step,
    Target,
)
from cua.artifact.schema import Ladder as _ArtifactLadder
from cua.policy.allowlist import NeedsApproval, Policy, check
from cua.policy.redaction import MASK
from cua.surface.a11y import normalize
from cua.surface.conditions import (
    SELF,
    Condition,
    LocationMatches,
    OutputExtracted,
    RegionPresent,
    TextPresent,
    ValueSet,
    Visible,
)
from cua.surface.locators import Ladder, Within, ladder_for
from cua.surface.protocol import (
    Action,
    Click,
    Node,
    Observation,
    Press,
    RecordingEnv,
    SelectOption,
    TypeText,
)

FAMILIES_DIR = Path("capabilities/families")
_LADDER: TypeAdapter[Ladder] = TypeAdapter(_ArtifactLadder)


class RecordError(ValueError):
    """The run cannot be turned into a capability, and why."""


# --------------------------------------------------------------------------
# Inputs: the run directory and the family template
# --------------------------------------------------------------------------


class _Loose(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _Param(_Loose):
    name: str
    type: str = "string"
    value: str


class _Output(_Loose):
    name: str
    type: str = "string"
    optional: bool = False
    description: str = ""


class _Goal(_Loose):
    goal: str
    name: str
    entry: str = "/"
    params: list[_Param] = Field(default_factory=list)
    outputs: list[_Output] = Field(default_factory=list)


class _Tenant(_Loose):
    id: str
    app_family: str
    base_url: str


class _CredentialRef(_Loose):
    ref: str
    fields: list[str]


class _RunJson(_Loose):
    """The parts of ``run.json`` the recorder reads."""

    run_id: str
    goal: _Goal
    tenant: _Tenant
    credentials: dict[str, _CredentialRef] = Field(default_factory=dict)
    provider: str
    model: str
    recording_env: RecordingEnv | None = None


class FamilyTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    vendor: str
    version_hint: str = ""
    surface: str = "web"


class FamilyTemplate(BaseModel):
    """What an app family contributes to every capability recorded against it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: FamilyTarget
    outcomes: dict[str, OutcomeSpec] = Field(default_factory=dict)
    outcome_detectors: list[Detector] = Field(default_factory=list)
    recovery_limits: RecoveryLimits = Field(default_factory=RecoveryLimits)
    recoverers: dict[str, Recoverer] = Field(default_factory=dict)
    redaction: Redaction = Field(default_factory=Redaction)


def transcript_sha256(log: bytes) -> str:
    """SHA-256 of a run's ``log.jsonl`` with line endings normalised to LF.

    The log is evidence that gets committed, and git may store it with other
    line endings than the machine that wrote it. The transcript is the same
    either way, so its hash must be too — or checking provenance against
    ``evidence/`` from a fresh clone would fail for no reason."""
    return hashlib.sha256(log.replace(b"\r\n", b"\n")).hexdigest()


def load_family(app_family: str, root: Path = FAMILIES_DIR) -> FamilyTemplate:
    path = root / f"{app_family}.yaml"
    if not path.is_file():
        raise RecordError(
            f"no template for app family {app_family!r} at {path.as_posix()}; "
            "a capability without one would have no outcome detectors"
        )
    return FamilyTemplate.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class _Run:
    """A run directory, read once."""

    def __init__(self, run_dir: Path) -> None:
        self.dir = run_dir
        if not (run_dir / "run.json").is_file():
            raise RecordError(f"{run_dir.as_posix()} is not a discovery run (no run.json)")
        self.run = _RunJson.model_validate_json((run_dir / "run.json").read_text("utf-8"))
        result_path = run_dir / "result.json"
        if not result_path.is_file():
            raise RecordError(f"run {self.run.run_id} has no result.json; it never finished")
        result = json.loads(result_path.read_text("utf-8"))
        if result.get("kind") != "done":
            reason = f" ({result['reason']})" if result.get("reason") else ""
            raise RecordError(
                f"run {self.run.run_id} ended {result.get('kind')}{reason}; "
                "only a run that reached done can be recorded"
            )
        self.log_bytes = (run_dir / "log.jsonl").read_bytes()
        lines = self.log_bytes.decode("utf-8").splitlines()
        self.events = [json.loads(line) for line in lines if line.strip()]
        self._observations: dict[str, Observation] = {}

    def observation(self, rel: str) -> Observation:
        if rel not in self._observations:
            text = (self.dir / rel).read_text("utf-8")
            self._observations[rel] = Observation.model_validate_json(text)
        return self._observations[rel]

    def ended_at(self) -> str:
        """When discovery reached done: the moment the run recorded the
        capability's behaviour, whenever it is turned into an artifact."""
        ends = [e["ts"] for e in self.events if e.get("event") == "run.end" and "ts" in e]
        if not ends:
            raise RecordError("the log has no run.end event")
        return str(ends[-1])

    def answering_model(self) -> str:
        """The model the provider says answered — which for an alias such as
        ``gemini-flash-latest`` is not the name that was asked for."""
        path = self.dir / "model_calls.jsonl"
        lines = path.read_text("utf-8").splitlines() if path.is_file() else []
        models = Counter(json.loads(line).get("model") for line in lines if line.strip())
        models.pop(None, None)
        if not models:
            return self.run.model
        return str(models.most_common(1)[0][0])


class _Acted(BaseModel):
    """One ``step`` event with the screens either side of it."""

    model_config = ConfigDict(frozen=True)

    tool: str
    node: Node | None
    ladder: Ladder | None
    text: str | None
    key: str | None
    before: Observation
    after: Observation | None


# --------------------------------------------------------------------------
# The recorder
# --------------------------------------------------------------------------


def record(
    run_dir: Path,
    *,
    policy: Policy,
    family: FamilyTemplate | None = None,
    families_dir: Path = FAMILIES_DIR,
    capability_id: str | None = None,
) -> Capability:
    """Read a finished discovery run and return a draft capability.

    Deterministic: the same run records to the same capability (so recording
    it twice is not a new version), apart from a fresh id. ``capability_id``
    exists so that a golden test can pin that too.
    would otherwise differ on every call.
    """
    run = _Run(run_dir)
    family = family or load_family(run.run.tenant.app_family, families_dir)
    params = {p.value: p.name for p in run.run.goal.params}

    acted, final, done_refs = _walk(run)
    steps: list[Step] = []
    checkpoints: list[Checkpoint] = []
    used: set[str] = set()
    cp_used: set[str] = set()
    for i, act in enumerate(acted):
        step = _step(act, params, policy, used, index=i + 1)
        steps.append(step)
        cp = _checkpoint_after(step, act, params, cp_used)
        if cp is not None:
            checkpoints.append(cp)

    outputs = _outputs(run, final, done_refs)
    if checkpoints:
        checkpoints[-1] = _with_output_regions(checkpoints[-1], final, done_refs)
    required = [n for n, o in outputs.items() if not o.optional] or list(outputs)
    if required:
        checkpoints.append(
            Checkpoint(
                id="cp.done",
                after_step=DONE,
                all_of=[OutputExtracted(name=n) for n in required],
            )
        )

    step_ids = {s.id for s in steps}
    checkpoint_ids = {c.id for c in checkpoints}
    recoverers = {
        name: rec
        for name, rec in family.recoverers.items()
        if all(s in step_ids for s in rec.sub_flow)
    }
    detectors = [
        d
        for d in family.outcome_detectors
        if _applies(d, step_ids, checkpoint_ids, set(recoverers))
    ]
    used_recoverers = {d.recover.run for d in detectors if d.recover and d.recover.run}
    recoverers = {n: r for n, r in recoverers.items() if n in used_recoverers}
    business = [d.code for d in detectors if d.class_ == "business"]

    irreversible = [s for s in steps if s.risk == "irreversible"]
    tenant = run.run.tenant
    goal = run.run.goal
    return Capability(
        id=capability_id or f"cap_{ULID()}",
        name=goal.name,
        description=goal.goal,
        target=Target(
            app_family=tenant.app_family,
            vendor=family.target.vendor,
            version_hint=family.target.version_hint,
            surface=family.target.surface,  # type: ignore[arg-type]
            entry=Entry(pattern="{tenant.base_url}/" + goal.entry.lstrip("/")),
        ),
        contract=Contract(
            side_effects="modifies_record" if irreversible else "none",
            idempotent=not irreversible,
            may_escalate=any(s.approval == "required" for s in steps),
            outcomes={c: family.outcomes.get(c, OutcomeSpec()) for c in business},
        ),
        credentials={
            name: CredentialSpec(ref=_portable_ref(c.ref, tenant.id), fields=c.fields)
            for name, c in run.run.credentials.items()
        },
        inputs={p.name: _input(p) for p in goal.params},
        outputs=outputs,
        recording_env=run.run.recording_env,
        steps=steps,
        checkpoints=checkpoints,
        outcome_detectors=detectors,
        recovery_limits=family.recovery_limits,
        recoverers=recoverers,
        redaction=family.redaction,
        provenance=Provenance(
            discovery_run_id=run.run.run_id,
            provider=run.run.provider,
            model=run.answering_model(),
            transcript_sha256=transcript_sha256(run.log_bytes),
            recorded_at=run.ended_at(),
            locator_rungs_used={
                **{s.id: s.target[0].strategy for s in steps if s.target},
                **{f"outputs.{n}": o.extract.target[0].strategy for n, o in outputs.items()},
            },
        ),
    )


def _walk(run: _Run) -> tuple[list[_Acted], Observation, dict[str, str]]:
    """Pair each step with the screen it acted on and the screen it left."""
    current: Observation | None = None
    pending: list[tuple[dict[str, Any], Observation]] = []
    acted: list[_Acted] = []
    done_refs: dict[str, str] | None = None
    final: Observation | None = None

    def close(after: Observation | None) -> None:
        for event, before in pending:
            acted.append(_acted(event, before, after, run))
        pending.clear()

    for event in run.events:
        kind = event.get("event")
        if kind == "observe" and "observation" in event:
            current = run.observation(event["observation"])
            close(current)
        elif kind == "step" and event.get("tool") != "read":
            if current is None:
                raise RecordError("a step was logged before any observation")
            pending.append((event, current))
        elif kind == "agent.done":
            done_refs = {name: o["ref"] for name, o in event.get("outputs", {}).items()}
            final = current
    close(None)

    if done_refs is None or final is None:
        raise RecordError("the log has no agent.done event")
    if not acted:
        raise RecordError("the run reached done without acting; there is nothing to replay")
    return acted, final, done_refs


def _acted(
    event: dict[str, Any], before: Observation, after: Observation | None, run: _Run
) -> _Acted:
    node = Node.model_validate(event["node"]) if event.get("node") else None
    ladder: Ladder | None = None
    if event.get("ladder"):
        ladder = _LADDER.validate_python(event["ladder"])
    elif node is not None:
        ladder = ladder_for(node, before) or None
    if node is not None and not ladder:
        raise RecordError(
            f"no locator names the {node.label} the agent acted on at turn "
            f"{event.get('turn')}; the step could not be found again"
        )
    return _Acted(
        tool=event["tool"],
        node=node,
        ladder=ladder,
        text=event.get("text"),
        key=event.get("key"),
        before=before,
        after=after,
    )


# -- steps --------------------------------------------------------------------


def _step(act: _Acted, params: dict[str, str], policy: Policy, used: set[str], index: int) -> Step:
    value = _template(act.text, params, index) if act.tool in ("type", "select") else None
    step_id = _unique(f"{_screen(act)}.{_control(act, value, used)}", used)
    risky = act.node is not None and isinstance(
        check(policy, _action(act), act.before), NeedsApproval
    )
    self_target = act.node is not None

    expect: Condition | None = None
    if act.tool in ("type", "select"):
        # A credential is compared by nobody: its field reads back masked.
        shown = value if value and "${credentials." not in value else None
        expect = ValueSet(target=SELF, value=shown)
    elif act.after is not None:
        changed = _changed_frame(act.before, act.after)
        if changed is not None:
            expect = _location(act.after, changed, params)

    return Step(
        id=step_id,
        action=act.tool,  # type: ignore[arg-type]
        target=act.ladder,
        value=value,
        key=act.key,
        wait_before=Visible(target=SELF) if self_target else None,
        expect_after=expect,
        risk="irreversible" if risky else "safe",
        approval="required" if risky else "none",
        retry=Retry(allowed=not risky),
        on_fail="escalate",
    )


def _template(text: str | None, params: dict[str, str], index: int) -> str:
    if text is None:
        raise RecordError(f"step {index} typed nothing")
    if MASK in text:
        raise RecordError(
            f"step {index} typed a masked value: a secret was typed literally rather than "
            "by ${credentials...} placeholder, so the artifact could not replay it"
        )
    if text in params:
        return f"${{{params[text]}}}"
    return text


def _action(act: _Acted) -> Action:
    assert act.node is not None
    ref = act.node.ref
    if act.tool == "type":
        return TypeText(ref=ref, text="")
    if act.tool == "select":
        return SelectOption(ref=ref, value="")
    if act.tool == "press":
        return Press(key=act.key or "", ref=ref)
    return Click(ref=ref)


def _screen(act: _Acted) -> str:
    """The screen a step is on, by the path of the frame holding its control:
    ``/login`` → ``login``, ``/member/10003`` → ``member``."""
    frame = act.node.frame if act.node else ""
    info = act.before.frame_info(frame)
    url = info.url if info else act.before.location
    segments = [s for s in urlsplit(url).path.split("/") if s]
    return _slug(segments[0]) if segments else "home"


def _control(act: _Acted, value: str | None, used: set[str]) -> str:
    """What the step does on its screen: the credential field or input it
    types, ``submit`` for the first button, else the control's words."""
    if act.tool in ("type", "select") and value:
        found = PLACEHOLDER.fullmatch(value)
        if found:
            return found.group(1).rsplit(".", 1)[-1]
    if act.tool == "press":
        return "press_" + _slug(act.key or "key")
    node = act.node
    assert node is not None
    if act.tool == "click" and node.role == "button":
        screen = _screen(act)
        if f"{screen}.submit" not in used:
            return "submit"
    return _slug(node.name or node.near_text or node.role)


def _unique(step_id: str, used: set[str]) -> str:
    candidate, n = step_id, 1
    while candidate in used:
        n += 1
        candidate = f"{step_id}_{n}"
    used.add(candidate)
    return candidate


# -- checkpoints --------------------------------------------------------------


def _frames(obs: Observation) -> dict[str, str]:
    return {f.name: f.url for f in obs.frames}


def _changed_frame(before: Observation, after: Observation) -> str | None:
    """The frame a step navigated: the first new or re-addressed frame that
    carries a heading, else the first that carries anything."""
    old = _frames(before)
    changed = [name for name, url in _frames(after).items() if old.get(name) != url]
    for name in changed:
        if any(n.role == "heading" for n in after.in_frame(name)):
            return name
    return next((name for name in changed if after.in_frame(name)), None)


def _location(obs: Observation, frame: str, params: dict[str, str]) -> LocationMatches:
    info = obs.frame_info(frame)
    url = info.url if info else obs.location
    parts = []
    for segment in urlsplit(url).path.split("/"):
        if segment in params or segment.isdigit():
            parts.append("[0-9]+" if segment.isdigit() else "[^/]+")
        else:
            parts.append(re.escape(segment))
    pattern = "/".join(parts) + "$"
    return LocationMatches(pattern=pattern, within=_within(frame))


def _checkpoint_after(
    step: Step, act: _Acted, params: dict[str, str], used: set[str]
) -> Checkpoint | None:
    if act.after is None or not isinstance(step.expect_after, LocationMatches):
        return None
    frame = _changed_frame(act.before, act.after)
    if frame is None:
        return None
    nodes = act.after.in_frame(frame)
    headings = [n.name for n in nodes if n.role == "heading" and n.name]
    conditions: list[Condition] = [step.expect_after]
    if headings:
        conditions.append(RegionPresent(name=headings[0], within=_within(frame)))
    texts = {normalize(n.text) for n in nodes if n.text}
    for value, name in params.items():
        if normalize(value) in texts:
            conditions.append(TextPresent(text=f"${{{name}}}", within=_within(frame)))

    # Leaving the sign-on screen is the checkpoint the family template's
    # session detectors are anchored to, whatever the next screen is called.
    base = "logged_in" if step.id.startswith("login.") else _slug(headings[0] if headings else "")
    cp_id = _unique(f"cp.{base or _screen(act)}", used)
    return Checkpoint(id=cp_id, after_step=step.id, all_of=conditions)


def _with_output_regions(cp: Checkpoint, final: Observation, refs: dict[str, str]) -> Checkpoint:
    """Add the heading above each output: "the Balances table is here", not
    just "a Member Detail page is here"."""
    present = {c.name for c in cp.all_of if isinstance(c, RegionPresent)}
    extra: list[Condition] = []
    for ref in refs.values():
        node = final.find(ref)
        if node is None:
            continue
        heading = _heading_above(final, node)
        if heading is not None and heading.name not in present:
            present.add(heading.name)
            extra.append(RegionPresent(name=heading.name, within=_within(node.frame)))
    if not extra:
        return cp
    return cp.model_copy(update={"all_of": [*cp.all_of, *extra]})


def _heading_above(obs: Observation, node: Node) -> Node | None:
    found: Node | None = None
    for candidate in obs.in_frame(node.frame):
        if candidate.ref == node.ref:
            return found
        if candidate.role == "heading" and candidate.name:
            found = candidate
    return None


# -- outputs, inputs, template -------------------------------------------------


def _outputs(run: _Run, final: Observation, refs: dict[str, str]) -> dict[str, OutputSpec]:
    done = {o["name"]: o for o in _done_outputs(run)}
    out: dict[str, OutputSpec] = {}
    for spec in run.run.goal.outputs:
        ref = refs.get(spec.name)
        if ref is None:
            if not spec.optional:
                raise RecordError(f"required output {spec.name!r} was not delivered by done")
            continue
        node = final.find(ref)
        if node is None:
            raise RecordError(f"output {spec.name!r} names {ref}, which is not on the final screen")
        ladder = ladder_for(node, final, for_value=True)
        if not ladder:
            raise RecordError(f"no locator names output {spec.name!r} by position")
        value = str(done.get(spec.name, {}).get("value", node.text))
        out[spec.name] = OutputSpec(
            type=spec.type,  # type: ignore[arg-type]
            optional=spec.optional,
            description=spec.description,
            extract=Extract(target=ladder, parse=_parse_for(spec.type, value)),
        )
    return out


def _done_outputs(run: _Run) -> list[dict[str, Any]]:
    for event in reversed(run.events):
        if event.get("event") == "agent.done":
            return [{"name": n, **o} for n, o in event.get("outputs", {}).items()]
    return []


def _parse_for(kind: str, sample: str) -> Any:
    if kind == "decimal":
        return "currency_usd" if "$" in sample else "decimal"
    if kind == "integer":
        return "integer"
    return "text"


def _input(param: _Param) -> InputSpec:
    # An all-digit discovery value says the input is an id, not a name. The
    # length is not inferred: one example cannot say whether 6 digits is valid.
    digits = param.type == "string" and param.value.isdigit()
    return InputSpec(
        type=param.type,  # type: ignore[arg-type]
        required=True,
        pattern="^[0-9]+$" if digits else None,
    )


def _applies(det: Detector, steps: set[str], checkpoints: set[str], recoverers: set[str]) -> bool:
    if det.scope is not None:
        if det.scope.after_step is not None and det.scope.after_step not in steps:
            return False
        if det.scope.after_checkpoint is not None and det.scope.after_checkpoint not in checkpoints:
            return False
    return not (det.recover and det.recover.run and det.recover.run not in recoverers)


def _portable_ref(ref: str, tenant_id: str) -> str:
    """``secret://local/x`` → ``secret://{tenant.id}/x``: the artifact names
    which secret, the tenant binding says whose."""
    prefix = f"secret://{tenant_id}/"
    return "secret://{tenant.id}/" + ref[len(prefix) :] if ref.startswith(prefix) else ref


def _within(frame: str) -> Within | None:
    return Within(frame=frame) if frame else None


def _slug(text: str) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", text.lower()))
