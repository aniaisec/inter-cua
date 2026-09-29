"""What a task operates on: one capability, a workflow of them, or neither.

Both strategies read it. Replay runs the capability or the workflow; the
baseline is asked for the same outputs, typed the same way, with the same
credentials. A task with neither states its outputs itself and runs on the
baseline alone: something no capability does (fetching a file, sending one),
asked of a model to see what it does.

Nothing here knows about models: the replay strategy imports this module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from cua.artifact.schema import Capability, ValueType
from cua.artifact.store import load
from cua.benchmark.models import BenchmarkTask
from cua.registry.store import Registry
from cua.tenant import Tenant
from cua.workflow.models import load_workflow, parse_ref
from cua.workflow.planner import Plan, plan

CAPABILITIES_DIR = Path("capabilities")
DEFAULT_CREDENTIAL = "app_login"


@dataclass(frozen=True)
class Output:
    name: str
    type: ValueType
    optional: bool = False
    description: str = ""


@dataclass(frozen=True)
class Subject:
    name: str
    """What the baseline's run is recorded under."""
    capability: Capability | None = None
    path: Path | None = None
    """The capability's file, or the workflow's."""
    plan: Plan | None = None
    input_types: dict[str, ValueType] | None = None
    outputs: tuple[Output, ...] = ()
    credentials: dict[str, str] | None = None
    """Credential name → ``secret://{tenant.id}/...``; None: the tenant's only
    app secret, as ``app_login`` (what ``cua discover`` offers by default)."""

    def credential_refs(self, tenant: Tenant) -> dict[str, str]:
        if self.credentials is not None:
            return {n: r.replace("{tenant.id}", tenant.id) for n, r in self.credentials.items()}
        if len(tenant.app_secrets) != 1:
            return {}
        (key,) = tenant.app_secrets
        return {DEFAULT_CREDENTIAL: f"secret://{tenant.id}/{key}"}

    def version(self) -> str | None:
        """Exactly what replay ran: each capability's number and content hash
        (a workflow's steps in order, after the workflow file's own hash).
        None for a task with nothing to replay."""
        if self.capability is not None:
            return _cap_version(self.capability)
        if self.plan is not None and self.path is not None:
            digest = hashlib.sha256(self.path.read_bytes().replace(b"\r\n", b"\n"))
            steps = "; ".join(_cap_version(s.capability) for s in self.plan.steps)
            return f"{self.name} sha256:{digest.hexdigest()[:12]} [{steps}]"
        return None

    def input_type(self, name: str) -> ValueType:
        return (self.input_types or {}).get(name, "string")


def _cap_version(cap: Capability) -> str:
    return f"{cap.name} v{cap.version} sha256:{cap.content_hash()[:12]}"


def task_version(task: BenchmarkTask) -> str:
    """A fingerprint of the task as run: goal, inputs, conditions and truth.
    Editing any of them is a new task, even under the same id."""
    canonical = json.dumps(task.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def subject_for(task: BenchmarkTask, capabilities_dir: Path = CAPABILITIES_DIR) -> Subject:
    subject = _subject(task, capabilities_dir)
    if task.goal.outputs is not None:
        subject = replace(subject, outputs=_stated(task.goal.outputs))
    return subject


def _stated(outputs: dict[str, str]) -> tuple[Output, ...]:
    return tuple(
        Output(n, cast(ValueType, t.removesuffix("?")), optional=t.endswith("?"))
        for n, t in outputs.items()
    )


def _subject(task: BenchmarkTask, capabilities_dir: Path) -> Subject:
    if task.capability is not None:
        path = Path(task.capability)
        cap = load(path)
        return Subject(
            name=cap.name,
            capability=cap,
            path=path,
            input_types={n: s.type for n, s in cap.inputs.items()},
            outputs=tuple(
                Output(n, o.type, o.optional, o.description) for n, o in cap.outputs.items()
            ),
            credentials={n: c.ref for n, c in cap.credentials.items()},
        )
    if task.workflow is not None:
        path = Path(task.workflow)
        workflow = load_workflow(path)
        p = plan(workflow, Registry(capabilities_dir))
        outputs = []
        for name, text in workflow.outputs.items():
            ref = parse_ref(text)
            if ref.kind == "output" and ref.step is not None:
                spec = p.step(ref.step).capability.outputs[ref.name]
                outputs.append(Output(name, spec.type, spec.optional, spec.description))
            elif ref.kind == "input":
                outputs.append(Output(name, workflow.inputs[ref.name].type))
        credentials: dict[str, str] = {}
        for step in p.steps:
            credentials.update({n: c.ref for n, c in step.capability.credentials.items()})
        return Subject(
            name=workflow.name,
            path=path,
            plan=p,
            input_types={n: s.type for n, s in workflow.inputs.items()},
            outputs=tuple(outputs),
            credentials=credentials,
        )
    return Subject(name=task.id.replace("-", "_"))
