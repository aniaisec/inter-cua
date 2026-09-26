"""The workflow file, its references, and what a workflow run returns.

``workflows/<name>.yaml``::

    name: open_member_subaccount
    version: 1
    description: Look the member up, then open a savings sub-account.
    inputs:
      member_id: {type: string, pattern: "^[0-9]+$"}
      initial_deposit: {type: decimal}
    steps:
      - id: lookup
        capability: member_savings_balance
        inputs:
          member_id: ${member_id}
      - id: open
        capability: open_subaccount
        version: 3                       # optional pin; default: highest approved
        inputs:
          member_id: ${member_id}
          initial_deposit: ${initial_deposit}
    outputs:
      member_name: ${lookup.output.member_name}
      reference_number: ${open.output.reference_number}

A binding is exactly one of: ``${input}`` (a workflow input),
``${step.output.name}`` (an output of an earlier step), or a literal string
with no ``${`` in it. No templating: a value that mixed text and references
could not be type-checked before the run, and every reference is checked
before anything runs (``cua.workflow.validator``).

Inputs are declared with the capability's own ``InputSpec``, so a workflow
input is typed, patterned and marked sensitive exactly as a capability input
is.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cua.artifact.schema import InputSpec, Name
from cua.replay.result import OutputValue, ReplayResult, SideEffect

WORKFLOWS_DIR = Path("workflows")

_INPUT_REF = re.compile(r"^\$\{([a-z][a-z0-9_]*)\}$")
_OUTPUT_REF = re.compile(r"^\$\{([a-z][a-z0-9_]*)\.output\.([a-z][a-z0-9_]*)\}$")


class WorkflowError(ValueError):
    """The workflow cannot be loaded or planned, and why."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StepSpec(_Model):
    id: Name
    capability: Name
    version: Annotated[int, Field(ge=1)] | None = None
    """Pin a version. Default: the one a call by name runs, the highest approved."""
    inputs: dict[str, str] = Field(default_factory=dict)
    """Capability input name -> binding. Literals are strings, quoted in YAML:
    a YAML number would reach here as a float and lose its exact value."""


class Workflow(_Model):
    name: Name
    version: Annotated[int, Field(ge=1)] = 1
    description: str = ""
    inputs: dict[Name, InputSpec] = Field(default_factory=dict)
    steps: Annotated[list[StepSpec], Field(min_length=1)]
    outputs: dict[Name, str] = Field(default_factory=dict)
    """Workflow output name -> ``${step.output.name}`` or ``${input}``."""

    def content_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def step(self, step_id: str) -> StepSpec:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)


@dataclass(frozen=True)
class Ref:
    """One binding, parsed."""

    kind: Literal["input", "output", "literal"]
    name: str = ""
    """The workflow input, or the step's output."""
    step: str | None = None
    value: str | None = None
    """For a literal."""

    def __str__(self) -> str:
        if self.kind == "input":
            return f"${{{self.name}}}"
        if self.kind == "output":
            return f"${{{self.step}.output.{self.name}}}"
        return repr(self.value)


def parse_ref(text: str) -> Ref:
    if m := _INPUT_REF.match(text):
        return Ref("input", name=m.group(1))
    if m := _OUTPUT_REF.match(text):
        return Ref("output", name=m.group(2), step=m.group(1))
    if "${" in text:
        raise ValueError(
            f"{text!r} is not a binding: use exactly ${{input}} or ${{step.output.name}}, "
            "or a literal with no ${ in it"
        )
    return Ref("literal", value=text)


def load_workflow(path: Path) -> Workflow:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise WorkflowError(f"{path.as_posix()}: {exc}") from None
    except yaml.YAMLError as exc:
        raise WorkflowError(f"{path.as_posix()} is not YAML: {exc}") from None
    try:
        return Workflow.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'workflow'}: {e['msg']}" for e in exc.errors()
        )
        raise WorkflowError(f"{path.as_posix()}: {problems}") from None


def find_workflow(name_or_path: str, workflows_dir: Path = WORKFLOWS_DIR) -> Path:
    """A path as given, else ``<workflows_dir>/<name>.yaml``."""
    given = Path(name_or_path)
    if given.suffix in (".yaml", ".yml") or given.is_file():
        if not given.is_file():
            raise WorkflowError(f"no workflow at {given.as_posix()}")
        return given
    path = workflows_dir / f"{name_or_path}.yaml"
    if not path.is_file():
        known = (
            sorted(p.stem for p in workflows_dir.glob("*.yaml")) if workflows_dir.is_dir() else []
        )
        raise WorkflowError(
            f"no workflow {name_or_path!r} in {workflows_dir.as_posix()}"
            + (f" (there: {', '.join(known)})" if known else "")
        )
    return path


# --------------------------------------------------------------------------
# What a workflow run returns
# --------------------------------------------------------------------------

StepKind = Literal["success", "business_outcome", "failure", "escalated", "not_run"]
RefusalCode = Literal["INPUT_INVALID", "POLICY_BLOCKED", "TIMEOUT", "INTERRUPTED"]
"""The workflow's own codes, for a refusal before or between steps. They are
replay failure codes, so a caller reads them the same way."""


class StepResult(_Model):
    id: str
    capability: str
    version: int
    kind: StepKind
    code: str | None = None
    side_effect: SideEffect = "none"
    outputs: dict[str, OutputValue] = Field(default_factory=dict)
    idempotency_key: str | None = None
    """The key the step ran under, derived from the workflow's."""
    run_id: str | None = None
    run_dir: str | None = None
    cached: bool = False
    """Answered by the step's idempotency cache or an earlier attempt: nothing ran."""
    duration_ms: int = 0


class WorkflowResult(_Model):
    """One of the four replay kinds, for the whole workflow.

    ``kind`` is the first step that did not succeed (the workflow stops
    there), else ``success``. ``side_effect`` folds every step's: ``unknown``
    if any step's is, else ``committed`` if any step's is, else ``none`` —
    and ``steps`` says which step did what.
    """

    kind: Literal["success", "business_outcome", "failure", "escalated"]
    code: str | None = None
    """The ending step's failure or business code, or a ``RefusalCode``."""
    step_id: str | None = None
    """The workflow step that ended the run."""
    side_effect: SideEffect = "none"
    outputs: dict[str, OutputValue] = Field(default_factory=dict)
    message: str = ""
    workflow: str
    workflow_version: int
    workflow_run_id: str | None = None
    idempotency_key: str | None = None
    steps: list[StepResult] = Field(default_factory=list)
    resume_token: str | None = None
    """An escalated step's: ``cua resume`` it, then run the workflow again
    with the same idempotency key to carry on after it."""
    operator_url: str | None = None
    detail: ReplayResult | None = None
    """The ending step's own result, in full."""
    run_dir: str | None = None
    duration_ms: int = 0
    cached: bool = False


def to_json(result: WorkflowResult) -> str:
    first = ("kind", "code", "step_id", "side_effect", "outputs", "message")
    data = result.model_dump(mode="json")
    ordered = {k: data[k] for k in first}
    ordered.update((k, v) for k, v in data.items() if k not in ordered)
    return json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"
