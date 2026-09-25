"""Read a run directory into canonical events.

Every file a run leaves is read the same way: ``log.jsonl`` (what the engine or
the loop did and why), ``model_calls.jsonl`` (one line per model call),
``human_actions.jsonl`` (what a person did while holding the controls),
``state_transitions.jsonl`` (who held the controls when) and ``result.json``
(how it ended). The mapping from each legacy name to its canonical type is in
one table here, so the vocabulary can be read and argued with in one place.

A legacy line that answers no question the canonical vocabulary asks (a
screenshot mask being added, a stored observation) maps to nothing; it stays
in the log, which remains the full record.

Reading only: nothing here writes to a run directory. The files are evidence,
and a reader that "fixed" them would be tampering with it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cua.observability.correlation import Correlation, RunHeader, read_header
from cua.observability.cost import PriceTable, normalize_usage
from cua.observability.events import Event, EventType

LOG = "log.jsonl"
MODEL_CALLS = "model_calls.jsonl"
HUMAN_ACTIONS = "human_actions.jsonl"
TRANSITIONS = "state_transitions.jsonl"
RESULT = "result.json"

_ENDS: dict[str, EventType] = {
    # replay
    "success": "run.completed",
    "business_outcome": "run.completed",
    "failure": "run.failed",
    "escalated": "run.escalated",
    # discovery
    "done": "run.completed",
    "stopped": "run.failed",
    "error": "run.failed",
}
"""``run.end``'s ``kind``. A business outcome (NOT_FOUND, PERMISSION_DENIED)
is the capability working: the app gave a declared answer and the run read
it. It completes; it does not fail."""

# Legacy name -> canonical type, and the fields worth carrying. Names needing
# more than a rename (run.end, replay.failed, locator.resolved, ...) are
# handled in ``_from_log``.
_SIMPLE: dict[str, tuple[EventType, tuple[str, ...]]] = {
    "step.start": ("step.started", ("action", "mode")),
    "agent.decision": ("step.started", ("tool", "target", "reason")),
    "action.done": ("step.action", ("action", "tool", "ms")),
    "action.read": ("step.action", ("tool",)),
    "step.passed": ("step.completed", ()),
    "action.failed": ("step.failed", ("error",)),
    "agent.bad_call": ("step.failed", ("error",)),
    "replay.surface_error": ("step.failed", ("error",)),
    "locator.slip": ("locator.drift", ("warning",)),
    "action.refind": ("locator.drift", ("reason",)),
    "recovery.start": ("recovery.started", ("recoverer",)),
    "recovery.restart": ("recovery.started", ("code",)),
    "recovery.retry": ("recovery.started", ("code", "cause")),
    "recovery.done": ("recovery.completed", ("recoverer",)),
    "recovery.resumed": ("recovery.completed", ("after", "next")),
    "policy.block": ("policy.blocked", ("reason",)),
    "policy.needs_approval": ("policy.blocked", ("rule",)),
    "policy.approved": ("policy.checked", ("rule", "approved_by", "via")),
    "policy.auto_approved": ("policy.checked", ("rule", "reason")),
    "policy.draft_override": ("policy.checked", ("warning",)),
    "policy.deprecated": ("policy.checked", ("warning",)),
    "approval.spent": ("policy.checked", ()),
    "escalation.requested": ("human.handoff", ("reason", "code", "request", "attempt")),
    "handoff.handed_back": ("human.resumed", ("by", "decision", "human_actions", "request")),
    "handoff.aborted": ("human.aborted", ("by", "why", "human_actions", "request")),
    "irreversible.act": ("side_effect.detected", ()),
    "irreversible.committed": ("side_effect.committed", ("seen", "performed_by")),
}

_EXTRA_ATTRS: dict[str, dict[str, Any]] = {
    "action.refind": {"cause": "refind"},
    "recovery.restart": {"action": "restart"},
    "recovery.retry": {"action": "retry"},
    "recovery.start": {"action": "recoverer"},
    "policy.needs_approval": {"decision": "needs_approval"},
    "policy.block": {"decision": "blocked"},
    "policy.approved": {"decision": "approved"},
    "policy.auto_approved": {"decision": "auto_approved"},
    "policy.draft_override": {"decision": "draft_override"},
    "policy.deprecated": {"decision": "deprecated_version"},
    "approval.spent": {"decision": "token_spent"},
    "irreversible.act": {"phase": "attempted"},
    "action.read": {"action": "read"},
}

_HUMAN_STATES = ("PAUSED", "HUMAN_IN_CONTROL")
"""Control states in which the run waits on a person."""


@dataclass
class Transition:
    ts: str
    to: str


@dataclass
class RunRecord:
    """A run directory, read."""

    dir: Path
    header: RunHeader
    events: list[Event]
    result: dict[str, Any] | None
    transitions: list[Transition] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    """Lines that could not be read. A run killed mid-write leaves a torn last
    line; the rest of the run is still worth reading."""

    @property
    def run_id(self) -> str:
        return self.header.correlation.run_id

    def of(self, *types: EventType) -> list[Event]:
        return [e for e in self.events if e.type in types]


def is_run_dir(path: Path) -> bool:
    return (path / "run.json").is_file() and (path / LOG).is_file()


def read_run(run_dir: Path, prices: PriceTable | None = None) -> RunRecord:
    """``prices``: to put an estimated cost on each model call. Without one
    the calls carry tokens only."""
    header = read_header(run_dir)
    warnings: list[str] = []
    log = list(_jsonl(run_dir / LOG, warnings))
    calls = list(_jsonl(run_dir / MODEL_CALLS, warnings))
    humans = list(_jsonl(run_dir / HUMAN_ACTIONS, warnings))
    transitions = [
        Transition(ts=str(t["ts"]), to=str(t.get("to")))
        for t in _jsonl(run_dir / TRANSITIONS, warnings)
        if "ts" in t
    ]
    result = _json(run_dir / RESULT, warnings)

    make = _Maker(header.correlation)
    events: list[Event] = []
    for line in log:
        events.extend(_from_log(make, line))
    events.extend(_from_calls(make, calls, log, prices))
    events.extend(_from_humans(make, humans))
    events.extend(_from_result(make, result, log))
    # A stable sort: lines from one file keep their order when two share a
    # millisecond.
    events.sort(key=lambda e: e.timestamp)
    return RunRecord(
        dir=run_dir,
        header=header,
        events=events,
        result=result,
        transitions=transitions,
        warnings=warnings,
    )


def find_runs(roots: list[Path]) -> Iterator[Path]:
    """Every run directory under ``roots``, each run id once. Evidence is
    sometimes a copy of a run kept elsewhere; the first one found is used."""
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        candidates = (
            [root] if is_run_dir(root) else sorted(p.parent for p in root.rglob("run.json"))
        )
        for path in candidates:
            if not is_run_dir(path):
                continue
            run_id = _run_id_of(path)
            if run_id in seen:
                continue
            seen.add(run_id)
            yield path


def locate_run(run: str, roots: list[Path]) -> Path | None:
    """A run by directory path, or by id under ``roots``."""
    as_path = Path(run)
    if is_run_dir(as_path):
        return as_path
    for root in roots:
        if not root.is_dir():
            continue
        direct = root / run
        if is_run_dir(direct):
            return direct
        for path in root.rglob(run):
            if is_run_dir(path):
                return path
    for path in find_runs(roots):
        if _run_id_of(path) == run:
            return path
    return None


# -- the mapping ---------------------------------------------------------------


class _Maker:
    def __init__(self, corr: Correlation) -> None:
        self.corr = corr

    def __call__(
        self,
        type_: EventType,
        ts: str,
        source: str,
        *,
        step: str | None = None,
        seq: int | None = None,
        attrs: dict[str, Any] | None = None,
        basis: str | None = None,
    ) -> Event:
        attrs = {k: v for k, v in (attrs or {}).items() if v is not None}
        if basis is not None:
            attrs["basis"] = basis
        return Event(
            type=type_,
            timestamp=ts,
            run_id=self.corr.run_id,
            invocation_id=self.corr.invocation_id,
            tenant_id=self.corr.tenant_id,
            capability_id=self.corr.capability_id,
            capability_version=self.corr.capability_version,
            step_id=step,
            source=source,
            source_seq=seq,
            attrs=attrs,
            derived=basis is not None,
        )


def _step_of(line: dict[str, Any]) -> str | None:
    if line.get("step") is not None:
        return str(line["step"])
    if line.get("turn") is not None:
        return f"turn:{line['turn']}"
    return None


def _from_log(make: _Maker, line: dict[str, Any]) -> list[Event]:
    name = str(line.get("event"))
    ts = str(line.get("ts"))
    seq = line.get("seq")
    source = f"{LOG}:{name}"
    step = _step_of(line)

    def ev(
        type_: EventType, attrs: dict[str, Any] | None = None, *, at: str | None = step
    ) -> Event:
        return make(type_, ts, source, step=at, seq=seq, attrs=attrs)

    if name in ("run.start", "run.resumed"):
        return [
            ev(
                "run.started",
                {
                    "resumed": name == "run.resumed",
                    "provider": line.get("provider"),
                    "model": line.get("model"),
                    "request": line.get("request"),
                },
            )
        ]
    if name == "run.end":
        kind = str(line.get("kind"))
        type_ = _ENDS.get(kind, "run.failed")
        return [
            ev(
                type_,
                {
                    "kind": kind,
                    "code": line.get("code"),
                    "reason": line.get("reason"),
                    "message": line.get("message"),
                },
            )
        ]
    if name in ("replay.failed", "replay.fault"):
        code = line.get("code")
        attrs = {
            "code": code,
            "detector": line.get("detector"),
            "expected": line.get("expected"),
            "message": line.get("message"),
            "handed_over": name == "replay.fault",
        }
        out = [ev("step.failed", attrs)]
        if code == "LOCATOR_UNRESOLVED":
            out.append(ev("locator.failed", {"code": code, "message": line.get("message")}))
        if code == "RECOVERY_EXHAUSTED":
            out.append(ev("recovery.exhausted", {"code": code, "message": line.get("message")}))
        return out
    if name == "locator.resolved":
        target = str(line.get("target"))
        rung, recorded = line.get("rung"), line.get("recorded")
        fell_back = recorded is not None and rung != recorded
        out = [
            ev(
                "locator.resolved",
                {"rung": rung, "recorded": recorded, "fell_back": fell_back},
                at=target,
            )
        ]
        if fell_back:
            out.append(
                ev("locator.drift", {"cause": "fallback", "from": recorded, "to": rung}, at=target)
            )
        return out
    if name == "model.error":
        return [ev("llm.completed", {"error": line.get("error")})]
    simple = _SIMPLE.get(name)
    if simple is None:
        return []
    type_, keep = simple
    attrs = {k: line.get(k) for k in keep}
    attrs.update(_EXTRA_ATTRS.get(name, {}))
    return [ev(type_, attrs)]


def _from_calls(
    make: _Maker,
    calls: list[dict[str, Any]],
    log: list[dict[str, Any]],
    prices: PriceTable | None,
) -> list[Event]:
    """Each call ends at its line's ``ts``. Its start is measured (``ms``,
    written since the loop started timing its calls) or, for older runs,
    inferred: the model was asked right after the last thing the loop logged
    before the answer came back (the observation it was shown)."""
    log_ts = sorted(str(line.get("ts")) for line in log if line.get("ts"))
    out: list[Event] = []
    for i, call in enumerate(calls):
        ts = str(call.get("ts"))
        provider = call.get("provider")
        model = call.get("model")
        usage = normalize_usage(provider, call.get("usage") or {})
        step = f"turn:{call['turn']}" if call.get("turn") is not None else None
        source = f"{MODEL_CALLS}:{i + 1}"
        ms = call.get("ms")
        if isinstance(ms, (int, float)):
            started, basis = _minus_ms(ts, float(ms)), None
        else:
            before = [t for t in log_ts if t < ts]
            started = before[-1] if before else ts
            basis = "inferred: the last log line before the answer"
            ms = _ms_between(started, ts)
        cost = prices.cost_of(provider, model, usage) if prices is not None else None
        out.append(make("llm.started", started, source, step=step, basis=basis))
        out.append(
            make(
                "llm.completed",
                ts,
                source,
                step=step,
                attrs={
                    "provider": provider,
                    "model": model,
                    "response_id": call.get("response_id"),
                    "stop_reason": call.get("stop_reason"),
                    "ms": round(ms),
                    "ms_measured": basis is None,
                    "usage": usage,
                    "cost_usd": str(cost) if cost is not None else None,
                    "priced": cost is not None,
                },
            )
        )
    return out


def _from_humans(make: _Maker, humans: list[dict[str, Any]]) -> list[Event]:
    out: list[Event] = []
    for i, h in enumerate(humans):
        target = h.get("target")
        url = h.get("url")
        out.append(
            make(
                "human.action",
                str(h.get("ts")),
                f"{HUMAN_ACTIONS}:{i + 1}",
                attrs={
                    "source": h.get("source"),
                    "action": h.get("action"),
                    "by": h.get("by"),
                    "request": h.get("request_id"),
                    "target": target if isinstance(target, dict) else None,
                    "url": url.split("?")[0] if isinstance(url, str) else None,
                },
            )
        )
    return out


def _from_result(
    make: _Maker, result: dict[str, Any] | None, log: list[dict[str, Any]]
) -> list[Event]:
    """A run that ended not knowing whether its commit landed says so only in
    its result; that is the one fact worth an event of its own."""
    if not result or result.get("side_effect") != "unknown":
        return []
    ends = [line for line in log if line.get("event") == "run.end"]
    ts = str(ends[-1]["ts"]) if ends else str(log[-1]["ts"]) if log else ""
    return [
        make(
            "side_effect.unknown",
            ts,
            f"{RESULT}:side_effect",
            step=result.get("step_id"),
            attrs={"code": result.get("code")},
            basis="the result reports side_effect: unknown",
        )
    ]


# -- reading -------------------------------------------------------------------


def _jsonl(path: Path, warnings: list[str]) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            warnings.append(f"{path.name}:{n}: not JSON (a torn write?)")
            continue
        if isinstance(record, dict):
            yield record


def _json(path: Path, warnings: list[str]) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        warnings.append(f"{path.name}: not JSON")
        return None
    return data if isinstance(data, dict) else None


def _run_id_of(path: Path) -> str:
    try:
        return read_header(path).correlation.run_id
    except ValueError:
        return path.name


# -- time ----------------------------------------------------------------------


def parse_ts(ts: str) -> float:
    """Seconds since the epoch, from the logs' ``...T12:34:56.789Z``."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def format_ts(seconds: float) -> str:
    stamp = datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z")


def _minus_ms(ts: str, ms: float) -> str:
    return format_ts(parse_ts(ts) - ms / 1000)


def _ms_between(a: str, b: str) -> float:
    return max(0.0, (parse_ts(b) - parse_ts(a)) * 1000)
