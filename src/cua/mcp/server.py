"""The Model Context Protocol over stdio: JSON-RPC 2.0, one message per line.

What an MCP client needs from a tool server, and nothing more: ``initialize``,
``ping``, ``tools/list`` and ``tools/call``; notifications are accepted and
need no answer. Implemented here rather than through an SDK because the
protocol surface is this small, and because every call ends in
``cua.api.service``, which already owns authorization, idempotency and the
run lifecycle.

A tool call is answered with a tool result, never a protocol error, whenever
the model can do something about it: a refusal (``isError`` with the code and
why), a failure (``isError``, with its side effect), an escalation (what has
to happen next). Protocol errors are for what the client got wrong: an unknown
method or tool, a malformed request.

Tool calls run on their own threads, so a client can ``ping`` or call a
second tool while a replay takes its seconds; answers are written one whole
line at a time.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from decimal import Decimal
from typing import IO, Any

from pydantic import ValidationError
from ulid import ULID

from cua import __version__, catalog
from cua.api.access import Caller
from cua.api.models import (
    AbortRequest,
    ApiError,
    ApiRun,
    ApproveRequest,
    HandoffOptions,
    ResumeRequest,
    RunRequest,
)
from cua.api.service import MAX_WAIT_S, RunService
from cua.artifact.schema import Capability
from cua.mcp import tools
from cua.replay.invocation import ApprovalGrant

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
"""Newest first. A client asking for one of these gets it; any other gets the
newest, and decides for itself whether it can go on."""

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

INSTRUCTIONS = (
    "Each tool is an approved business capability of a legacy application, run "
    "deterministically with no model. Call it with typed arguments; the answer's `kind` is "
    "success, business_outcome (an answer about the business, not an error), failure (read "
    "side_effect before retrying) or escalated (read required_action). A tool that changes "
    "something needs an idempotency_key: reuse it to retry, never to repeat. A commit needs "
    "consent from a person; the run tools carry an escalated run on."
)

Handoff = str
"""``consent``: capabilities that may ask for consent are run with a handoff,
so that a commit without a token comes back ``escalated`` with the browser
waiting. ``all``: every capability, so that anything a person could fix comes
back ``escalated``. ``none``: nothing is handed to anyone; those faults are
failures."""


class ProtocolError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class McpServer:
    def __init__(
        self,
        service: RunService,
        caller: Callable[[str], Caller],
        *,
        wait_s: float = MAX_WAIT_S,
        handoff: Handoff = "consent",
        handoff_ttl_s: float = 1800.0,
    ) -> None:
        """``caller``: the identity every call acts as, given a request id. Over
        stdio the client is whoever started this process; ``cua mcp`` names it
        from the access file (``--client``), for its tenants, capabilities and
        scopes."""
        self.service = service
        self._caller = caller
        self.wait_s = wait_s
        self.handoff = handoff
        self.handoff_ttl_s = handoff_ttl_s

    # -- the transport --------------------------------------------------------

    def serve(self, stdin: IO[str], stdout: IO[str]) -> None:
        """Read requests until the client closes stdin, then let every call
        under way finish (a replay stopped mid-step is how a commit ends up
        ``unknown``)."""
        lock = threading.Lock()
        running: list[threading.Thread] = []

        def send(message: dict[str, Any]) -> None:
            line = json.dumps(message, ensure_ascii=False, default=str)
            with lock:
                stdout.write(line + "\n")
                stdout.flush()

        def answer(message: Any) -> None:
            response = self.handle(message)
            if response is not None:
                send(response)

        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line, parse_float=Decimal)
            except json.JSONDecodeError as exc:
                send(_error(None, PARSE_ERROR, f"not JSON: {exc}"))
                continue
            if isinstance(message, dict) and message.get("method") == "tools/call":
                worker = threading.Thread(target=answer, args=(message,), name="cua-mcp-call")
                worker.start()
                running.append(worker)
                running[:] = [t for t in running if t.is_alive()]
            else:
                answer(message)
        for worker in running:
            worker.join()

    def handle(self, message: Any) -> dict[str, Any] | None:
        """The response to one message; None for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "expected one JSON-RPC 2.0 object")
        method = message.get("method")
        request_id = message.get("id")
        is_request = "id" in message
        if not isinstance(method, str):
            return _error(request_id, INVALID_REQUEST, "no method") if is_request else None
        if not is_request:
            return None  # notifications/initialized, cancelled, ...: nothing to answer
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object")
        try:
            result = self._dispatch(method, params)
        except ProtocolError as exc:
            return _error(request_id, exc.code, exc.message)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "inter-cua", "title": "inter-cua", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            caller = self._caller(_request_id())
            listed = [tools.capability_tool(cap, caller) for cap in self._capabilities(caller)]
            return {"tools": listed + tools.run_tools(caller)}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(arguments, dict):
                raise ProtocolError(INVALID_PARAMS, "tools/call takes a name and arguments")
            return self.call(name, arguments)
        raise ProtocolError(METHOD_NOT_FOUND, f"no method {method!r}")

    # -- tools ------------------------------------------------------------------

    def _capabilities(self, caller: Caller) -> list[Capability]:
        """What ``tools/list`` offers, and so all ``tools/call`` will run."""
        entries, _ = catalog.scan(self.service.settings.capabilities_dir)
        return [
            e.capability
            for e in entries
            if e.invocable
            and caller.tenant.refusal(e.capability.name, e.capability.target.app_family) is None
            and caller.client.may_use(e.capability.name)
            and e.capability.name not in tools.RUN_TOOLS
            and not tools.RESERVED & set(e.capability.inputs)
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        caller = self._caller(_request_id())
        status = "error"
        try:
            if name in tools.RUN_TOOLS:
                record = self._run_tool(caller, name, arguments)
            else:
                cap = next((c for c in self._capabilities(caller) if c.name == name), None)
                if cap is None:
                    raise ProtocolError(INVALID_PARAMS, f"unknown tool {name!r}")
                record = self._invoke(caller, cap, arguments)
            view = tools.agent_view(record)
            failed = view["state"] in ("error", "lost") or view.get("kind") == "failure"
            status = "failure" if failed else "ok"
            return {
                "content": [
                    {"type": "text", "text": tools.summary(view)},
                    {"type": "text", "text": json.dumps(view, indent=2, ensure_ascii=False)},
                ],
                "structuredContent": view,
                "isError": failed,
            }
        except ApiError as exc:
            return _refused(exc.code, exc.message)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}"
                for e in exc.errors()
            )
            return _refused("invalid_arguments", problems)
        finally:
            self.service.audit(
                {
                    "request_id": caller.request_id,
                    "method": "MCP",
                    "path": f"tools/call {name}",
                    "status": status,
                    "client": caller.client_id,
                    "tenant": caller.tenant.id,
                }
            )

    def _invoke(self, caller: Caller, cap: Capability, arguments: dict[str, Any]) -> ApiRun:
        inputs = dict(arguments)
        key = inputs.pop(tools.IDEMPOTENCY_KEY, None)
        token = inputs.pop(tools.APPROVAL_TOKEN, None)
        if key is not None and not isinstance(key, str):
            raise ApiError(400, "invalid_arguments", "idempotency_key is a string")
        if token is not None and not isinstance(token, str):
            raise ApiError(400, "invalid_arguments", "approval_token is a string")
        hand_off = self.handoff == "all" or (
            self.handoff == "consent" and cap.contract.may_escalate
        )
        body = RunRequest(
            capability=cap.name,
            version=cap.version,
            inputs=inputs,
            approval=ApprovalGrant(token=token) if token else None,
            handoff=HandoffOptions(ttl_s=self.handoff_ttl_s) if hand_off else None,
        )
        record, _ = self.service.submit(caller, body, key or None)
        return self._settled(caller, record.run_id, self.wait_s)

    def _run_tool(self, caller: Caller, name: str, arguments: dict[str, Any]) -> ApiRun:
        args = dict(arguments)
        run_id = args.pop("run_id", None)
        if not isinstance(run_id, str) or not run_id:
            raise ApiError(400, "invalid_arguments", "run_id is required")
        if name == tools.STATUS:
            caller.require("read")
            wait = args.pop("wait_s", 0)
            if not isinstance(wait, int | float | Decimal) or isinstance(wait, bool):
                raise ApiError(400, "invalid_arguments", "wait_s is a number of seconds")
            _no_more(name, args)
            self.service.get(caller, run_id)
            return self._settled(caller, run_id, float(wait))
        if name == tools.APPROVE:
            request = ApproveRequest(
                token=args.pop("approval_token", ""),
                approved_by=args.pop("approved_by", None),
                inputs=args.pop("inputs", {}),
            )
            _no_more(name, args)
            self.service.approve(caller, run_id, request)
        elif name == tools.RESUME:
            resume = ResumeRequest(
                resume_at=args.pop("resume_at", None), inputs=args.pop("inputs", {})
            )
            _no_more(name, args)
            self.service.resume(caller, run_id, resume)
        else:
            abort = AbortRequest(why=args.pop("why", ""))
            _no_more(name, args)
            return self.service.abort(caller, run_id, abort)
        return self._settled(caller, run_id, self.wait_s)

    def _settled(self, caller: Caller, run_id: str, wait_s: float) -> ApiRun:
        self.service.wait(run_id, wait_s)
        return self.service.get(caller, run_id)


def _no_more(tool: str, args: dict[str, Any]) -> None:
    if args:
        raise ApiError(400, "invalid_arguments", f"{tool} takes no {sorted(args)}")


def _refused(code: str, message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": f"refused ({code}): {message}"}],
        "structuredContent": {"error": {"code": code, "message": message}},
        "isError": True,
    }


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _request_id() -> str:
    return f"mcp_{ULID()}"
