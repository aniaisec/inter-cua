"""The tools an agent sees, and what a tool call answers.

**Capability tools.** One per capability that unattended replay would run
(approved, on a surface this build drives), for the tenant's application
family, that the client is authorized for. The name is the capability's, the
input schema its typed inputs (``cua catalog --json``), and the description
its contract: purpose, outputs, side effect, idempotency, business outcomes
and escalations. Two arguments are the invocation's, not the capability's:

* ``idempotency_key`` — required on a capability that is not read-only: a
  retry with the same key returns the first run instead of committing again;
* ``approval_token`` — consent for a commit (``cua approval-token``), offered
  only on a capability that may ask for it and only to a client with the
  ``approve`` scope.

**Run tools.** ``cua_run_status``, ``cua_approve_run``, ``cua_resume_run`` and
``cua_abort_run`` act on a run a capability tool started, each offered only to
a client with the scope it needs. They carry an escalated run on; they never
reach into the GUI.

**Results.** The ``ReplayResult``, reduced to what an agent acts on: the
kind, the code or reason, the outputs, the side effect and the message. Locator
rungs, screenshots, observed screen text and evidence paths stay in the run
directory, under ``run_id``. An escalated run adds ``resume_token`` and
``required_action``: what has to happen, and which tool carries it on.
"""

from __future__ import annotations

from typing import Any

from cua import catalog
from cua.api.access import Caller
from cua.api.models import ApiRun
from cua.artifact.schema import Capability

IDEMPOTENCY_KEY = "idempotency_key"
APPROVAL_TOKEN = "approval_token"
RESERVED = frozenset({IDEMPOTENCY_KEY, APPROVAL_TOKEN})

STATUS = "cua_run_status"
APPROVE = "cua_approve_run"
RESUME = "cua_resume_run"
ABORT = "cua_abort_run"
RUN_TOOLS = (STATUS, APPROVE, RESUME, ABORT)

MCP_RESUME = (
    f"get an approval token for these exact inputs from someone who may consent, and call "
    f"{APPROVE} with the run_id"
)

AGENT_FIELDS = (
    "kind",
    "code",
    "reason",
    "step_id",
    "side_effect",
    "outputs",
    "payload",
    "message",
    "capability",
    "capability_version",
    "idempotency_key",
    "cached",
    "request_id",
    "resume_token",
    "operator_url",
    "during_recovery",
    "escalation_reason",
    "warnings",
)
"""What an agent is told about a result. Everything else (locator rungs,
screenshots, the observed screen text, evidence paths) is how the GUI was
driven, and stays in the run directory."""


def needs_key(cap: Capability) -> bool:
    return cap.contract.side_effects != "none" or not cap.contract.idempotent


def offers_approval(cap: Capability, caller: Caller) -> bool:
    return cap.contract.may_escalate and "approve" in caller.client.scopes


def capability_tool(cap: Capability, caller: Caller) -> dict[str, Any]:
    definition = catalog.tool_definition(cap, resume=MCP_RESUME)
    schema: dict[str, Any] = definition["input_schema"]
    properties = dict(schema["properties"])
    required = list(schema["required"])
    if needs_key(cap):
        properties[IDEMPOTENCY_KEY] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "Your id for this request. Calling again with the same key and "
            "arguments returns the first run's answer instead of doing it twice; use a new "
            "key for a new request.",
        }
        required.append(IDEMPOTENCY_KEY)
    if offers_approval(cap, caller):
        properties[APPROVAL_TOKEN] = {
            "type": "string",
            "description": "Consent for the commit, from someone who may give it (an "
            "approval token for exactly these inputs). Without one the run stops before "
            "committing and returns kind 'escalated'.",
        }
    c = cap.contract
    return {
        "name": cap.name,
        "title": cap.name.replace("_", " ").capitalize(),
        "description": definition["description"],
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": c.side_effects == "none",
            "destructiveHint": c.side_effects != "none",
            "idempotentHint": c.idempotent,
            "openWorldHint": True,
        },
    }


def run_tools(caller: Caller) -> list[dict[str, Any]]:
    run_id = {"type": "string", "description": "The run_id a capability tool returned"}
    wait = {
        "type": "number",
        "minimum": 0,
        "maximum": 300,
        "description": "Seconds to wait for the run to stand still (default 0)",
    }
    inputs = {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "description": "Sensitive inputs, supplied again: a run keeps only their hashes",
    }
    scopes = caller.client.scopes
    tools = []
    if "read" in scopes:
        tools.append(
            _run_tool(
                STATUS,
                "Where a run stands, and its result once it has one. A run still executing "
                "has state 'running'; wait_s holds the answer for it.",
                {"run_id": run_id, "wait_s": wait},
                read_only=True,
            )
        )
    if "approve" in scopes:
        tools.append(
            _run_tool(
                APPROVE,
                "Consent for the commit an escalated run stopped at (reason NEEDS_APPROVAL), "
                "and carry it on. approval_token is from someone who may consent, for exactly "
                "the run's inputs; one that is not is refused and the run keeps waiting.",
                {
                    "run_id": run_id,
                    "approval_token": {"type": "string", "minLength": 1},
                    "approved_by": {"type": "string"},
                    "inputs": inputs,
                },
                required=["run_id", "approval_token"],
            )
        )
    if "operate" in scopes:
        tools.append(
            _run_tool(
                RESUME,
                "Carry an escalated run on after a person has worked on it on the operator "
                "console: the run checks the screen against its checkpoints and goes on from "
                "the newest that holds.",
                {"run_id": run_id, "resume_at": {"type": "string"}, "inputs": inputs},
            )
        )
        tools.append(
            _run_tool(
                ABORT,
                "End an escalated run without finishing it. Its result says what it may have "
                "committed (side_effect).",
                {"run_id": run_id, "why": {"type": "string"}},
                destructive=True,
            )
        )
    return tools


def _run_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
    read_only: bool = False,
    destructive: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or ["run_id"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": read_only,
            "openWorldHint": not read_only,
        },
    }


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


def agent_view(record: ApiRun) -> dict[str, Any]:
    """A run as an agent is told about it."""
    view: dict[str, Any] = {"run_id": record.run_id, "state": record.state}
    result = record.result or {}
    view.update((k, result[k]) for k in AGENT_FIELDS if k in result and result[k] not in ([],))
    if record.error is not None:
        view["error"] = record.error
    action = required_action(record)
    if action is not None:
        view["required_action"] = action
    return view


def required_action(record: ApiRun) -> str | None:
    result = record.result or {}
    if record.state == "running":
        return f"the run is still executing; call {STATUS} with its run_id and wait_s"
    if record.state == "lost":
        return (
            "a person must read the run's evidence to find out what it did before anything "
            "is retried"
        )
    if record.state != "escalated":
        return None
    if result.get("reason") == "NEEDS_APPROVAL":
        return (
            f"consent is needed to commit: ask someone who may give it for an approval token "
            f"for exactly these inputs, then call {APPROVE} with run_id and approval_token. "
            f"To give up instead, call {ABORT}."
        )
    where = result.get("operator_url") or "the operator console (`cua operator`)"
    return (
        f"a person must look at the application: the request is open at {where}. Once they "
        f"have handed back, call {RESUME} with run_id; to give up, call {ABORT}."
    )


def summary(view: dict[str, Any]) -> str:
    """One line a model reads first, before the structured result."""
    kind = view.get("kind")
    if view["state"] == "error":
        return f"The request could not be carried out: {view.get('error', '')}"
    if view["state"] in ("running", "lost"):
        return f"Run {view['run_id']} is {view['state']}: {view.get('required_action', '')}"
    if kind == "success":
        return f"success: {view.get('outputs', {})} (side effect: {view.get('side_effect')})"
    if kind == "business_outcome":
        return f"business outcome {view.get('code')}: {view.get('message', '')}".rstrip(": ")
    if kind == "escalated":
        return f"escalated ({view.get('reason')}): {view.get('required_action', '')}"
    return (
        f"failure {view.get('code')} at {view.get('step_id')}: {view.get('message', '')} "
        f"(side effect: {view.get('side_effect')}; read it before retrying)"
    )
