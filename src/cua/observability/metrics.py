"""One run, explained: what happened, why, how long each part took, how many
model calls it made and what they cost, which locators found the controls,
whether it recovered, and whether a person stepped in.

Everything is computed from the canonical events (``cua.observability
.recorder``), never from what a run says about itself alone, and nothing is
stored: ask again after a change and the answer is recomputed from evidence.

**Where the time went.** A run's wall clock is split into buckets that add up
to it. Each instant is given to the most specific thing happening at the time,
in this order:

``human``     the run was waiting on a person (paused, or a person held the
              controls);
``llm``       a model call was in flight;
``recovery``  a recovery (a retry, a restart, a recoverer sub-flow) was running;
``act``       the browser was performing an action (click, type, navigate);
``locate``    between a step starting and its action: finding the control,
              the policy check;
``verify``    between an action and its step passing: waiting for the page to
              settle and the expected checkpoint to hold;
``evidence``  between one step passing and the next starting: the observation
              and (masked) screenshot kept as evidence of the step;
``startup``   from the run being created to its first event: browser launch;
``other``     anything else (discovery's own bookkeeping, observing the page
              for the model, extracting outputs).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.observability.events import Event
from cua.observability.recorder import _HUMAN_STATES, RunRecord, format_ts, parse_ts

Status = Literal["completed", "failed", "escalated", "incomplete"]
Bucket = Literal[
    "human", "llm", "recovery", "act", "locate", "verify", "evidence", "startup", "other"
]
BUCKETS: tuple[Bucket, ...] = (
    "human",
    "llm",
    "recovery",
    "act",
    "locate",
    "verify",
    "evidence",
    "startup",
    "other",
)
_PRIORITY: dict[Bucket, int] = {b: i for i, b in enumerate(BUCKETS)}
_TERMINAL = ("run.completed", "run.failed", "run.escalated")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StepMetrics(_Model):
    step_id: str
    started_at: str
    duration_s: float
    mode: str | None = None
    """``run``, or ``recover`` for a step run again inside a recovery."""
    action: str | None = None
    status: Literal["completed", "failed", "unfinished"]
    rung: str | None = None
    """The locator rung that found the step's control."""
    fell_back: bool = False
    """Found by a rung other than the one recorded: the screen has drifted."""
    locate_s: float = 0.0
    act_s: float = 0.0
    verify_s: float = 0.0
    error_code: str | None = None


class LLMUsage(_Model):
    calls: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    wait_s: float = 0.0
    wait_measured: bool = True
    """False when some call's duration was inferred (runs logged before calls
    were timed)."""
    models: list[str] = Field(default_factory=list)
    """The models that answered (an alias resolves to a version)."""
    estimated_cost_usd: Decimal | None = Decimal(0)
    """None when a model that answered has no price: unpriced, never free.
    An estimate from the configured price table, not billing data."""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.thinking_tokens


class HumanMetrics(_Model):
    handoffs: int = 0
    actions: int = 0
    """Lines a person produced while holding the controls (clicks, navigations,
    console commands)."""
    resumed: int = 0
    aborted: int = 0
    wait_s: float = 0.0

    @property
    def intervened(self) -> bool:
        return self.actions > 0 or self.resumed > 0 or self.aborted > 0


class RunMetrics(_Model):
    run_id: str
    invocation_id: str
    kind: Literal["replay", "discovery"]
    tenant_id: str | None
    capability: str | None
    capability_id: str | None
    capability_version: int | None
    inject: str | None = None
    started_at: str
    ended_at: str
    duration_s: float

    status: Status
    outcome: str
    """``kind[:code]`` as the run ended: ``success``, ``business_outcome:NOT_FOUND``,
    ``failure:LOCATOR_UNRESOLVED``, ``done``, ``escalated:STUCK``."""
    why: str | None = None
    """The run's own account of its end: the failure message, the stop reason."""
    failed_step: str | None = None

    steps: list[StepMetrics] = Field(default_factory=list)
    actions: int = 0
    llm: LLMUsage = LLMUsage()
    locators: dict[str, str] = Field(default_factory=dict)
    """Target → the rung that found it (last time it was looked for)."""
    locator_fallbacks: int = 0
    locator_drift: int = 0
    locator_failures: int = 0
    recoveries: int = 0
    recovery_exhausted: bool = False
    policy_checks: int = 0
    policy_blocks: int = 0
    consent_missing: bool = False
    """Stopped at a step that commits a change because no approval came with
    the request: the policy doing its job for the caller, not the capability
    failing."""
    human: HumanMetrics = HumanMetrics()
    side_effect: str | None = None
    """``none``, ``committed`` or ``unknown``, as the run reported it; None
    when the run reports none (discovery) and no commit was seen."""
    time: dict[str, float] = Field(default_factory=dict)
    """Bucket → seconds; adds up to ``duration_s``. See the module doc."""
    event_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def run_metrics(record: RunRecord) -> RunMetrics:
    events = record.events
    header = record.header
    corr = header.correlation
    first = events[0].timestamp if events else header.started_at or ""
    start = min(filter(None, [header.started_at, first])) if first else ""
    end = events[-1].timestamp if events else start
    terminal = [e for e in events if e.type in _TERMINAL]
    last = terminal[-1] if terminal else None
    status: Status = (
        "incomplete"
        if last is None
        else "completed"
        if last.type == "run.completed"
        else "escalated"
        if last.type == "run.escalated"
        else "failed"
    )
    result = record.result or {}
    steps = _steps(events)
    locators: dict[str, str] = {}
    for e in record.of("locator.resolved"):
        if e.step_id is not None and e.attrs.get("rung"):
            locators[e.step_id] = str(e.attrs["rung"])
    failed = [e for e in record.of("step.failed")]
    return RunMetrics(
        run_id=corr.run_id,
        invocation_id=corr.invocation_id,
        kind=header.kind,
        tenant_id=corr.tenant_id,
        capability=corr.capability,
        capability_id=corr.capability_id,
        capability_version=corr.capability_version,
        inject=header.inject,
        started_at=start,
        ended_at=end,
        duration_s=_secs(start, end),
        status=status,
        outcome=_outcome(last),
        why=_why(last, result, failed),
        failed_step=failed[-1].step_id if failed and status != "completed" else None,
        steps=steps,
        actions=len(record.of("step.action")),
        llm=_llm(record),
        locators=locators,
        locator_fallbacks=sum(1 for e in record.of("locator.resolved") if e.attrs.get("fell_back")),
        locator_drift=len(record.of("locator.drift")),
        locator_failures=len(record.of("locator.failed")),
        recoveries=len(record.of("recovery.started")),
        recovery_exhausted=bool(record.of("recovery.exhausted")),
        policy_checks=len(record.of("policy.checked")),
        policy_blocks=len(record.of("policy.blocked")),
        consent_missing=_outcome(last) == "failure:POLICY_BLOCKED"
        and any(e.attrs.get("decision") == "needs_approval" for e in record.of("policy.blocked")),
        human=_human(record, end),
        side_effect=_side_effect(record, result),
        time=breakdown(record, start, end),
        event_counts=_counts(events),
        warnings=list(record.warnings),
    )


# -- parts -----------------------------------------------------------------------


def _outcome(last: Event | None) -> str:
    if last is None:
        return "incomplete"
    kind = str(last.attrs.get("kind", last.type.removeprefix("run.")))
    code = last.attrs.get("code") or last.attrs.get("reason")
    return f"{kind}:{code}" if code else kind


def _why(last: Event | None, result: dict[str, Any], failed: list[Event]) -> str | None:
    if last is None:
        return None
    for value in (
        result.get("message"),
        last.attrs.get("message"),
    ):
        if value:
            return str(value)
    if last.type == "run.completed" or not failed:
        return None
    for value in (failed[-1].attrs.get("message"), failed[-1].attrs.get("error")):
        if value:
            return str(value)
    return None


def _llm(record: RunRecord) -> LLMUsage:
    done = record.of("llm.completed")
    calls = [e for e in done if "error" not in e.attrs]
    usage: dict[str, int] = {}
    cost: Decimal | None = Decimal(0)
    for e in calls:
        for k, v in (e.attrs.get("usage") or {}).items():
            usage[k] = usage.get(k, 0) + int(v)
        if e.attrs.get("cost_usd") is None:
            cost = None
        elif cost is not None:
            cost += Decimal(str(e.attrs["cost_usd"]))
    if record.header.provider == "scripted":
        cost = Decimal(0)
    return LLMUsage(
        calls=len(calls),
        errors=len(done) - len(calls),
        input_tokens=usage.get("input", 0),
        output_tokens=usage.get("output", 0),
        thinking_tokens=usage.get("thinking", 0),
        cache_read_tokens=usage.get("cache_read", 0),
        cache_write_tokens=usage.get("cache_write", 0),
        wait_s=round(sum(float(e.attrs.get("ms", 0)) for e in calls) / 1000, 3),
        wait_measured=all(e.attrs.get("ms_measured", False) for e in calls),
        models=sorted({str(e.attrs["model"]) for e in calls if e.attrs.get("model")}),
        estimated_cost_usd=cost if calls else Decimal(0),
    )


def _human(record: RunRecord, end: str) -> HumanMetrics:
    waits = [(a, b) for a, b, _ in _human_intervals(record, end)]
    return HumanMetrics(
        handoffs=len(record.of("human.handoff")),
        actions=len(record.of("human.action")),
        resumed=len(record.of("human.resumed")),
        aborted=len(record.of("human.aborted")),
        wait_s=round(sum(b - a for a, b in waits), 3),
    )


def _side_effect(record: RunRecord, result: dict[str, Any]) -> str | None:
    reported = result.get("side_effect")
    if isinstance(reported, str):
        return reported
    if record.of("side_effect.committed"):
        return "committed"
    return None


def _counts(events: list[Event]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        counts[e.type] = counts.get(e.type, 0) + 1
    return dict(sorted(counts.items()))


# -- steps -----------------------------------------------------------------------


def _steps(events: list[Event]) -> list[StepMetrics]:
    """Replay steps (``step.started`` .. ``step.completed``/``failed``), and
    discovery turns (a decision, its action)."""
    out: list[StepMetrics] = []
    open_: dict[str, Any] | None = None

    def close(status: Literal["completed", "failed", "unfinished"], at: str, **extra: Any) -> None:
        nonlocal open_
        if open_ is None:
            return
        started = open_["started_at"]
        act_end = open_.get("act_end")
        act_s = open_.get("act_s", 0.0)
        resolved = open_.get("resolved_at")
        if act_end:
            # The action finished: its own measured time, and what came either side.
            locate_s = _secs(started, _secs_add(act_end, -act_s))
            verify_s = _secs(act_end, at)
        elif resolved:
            # The control was found; the action never reported back (it timed
            # out or the run stopped): from then on, it was being performed.
            locate_s, act_s, verify_s = _secs(started, resolved), _secs(resolved, at), 0.0
        else:
            locate_s, verify_s = _secs(started, at), 0.0
        out.append(
            StepMetrics(
                step_id=open_["step_id"],
                started_at=started,
                duration_s=_secs(started, at),
                mode=open_.get("mode"),
                action=open_.get("action"),
                status=status,
                rung=open_.get("rung"),
                fell_back=open_.get("fell_back", False),
                locate_s=locate_s,
                act_s=round(act_s, 3),
                verify_s=verify_s,
                **extra,
            )
        )
        open_ = None

    for e in events:
        if e.type == "step.started":
            close("unfinished" if e.attrs.get("tool") is None else "completed", e.timestamp)
            open_ = {
                "step_id": e.step_id or "?",
                "started_at": e.timestamp,
                "mode": e.attrs.get("mode"),
                "action": e.attrs.get("action") or e.attrs.get("tool"),
            }
        elif open_ is None:
            continue
        elif e.type == "locator.resolved" and e.step_id == open_["step_id"]:
            open_["rung"] = e.attrs.get("rung")
            open_["fell_back"] = bool(e.attrs.get("fell_back"))
            open_["resolved_at"] = e.timestamp
        elif e.type == "step.action" and "ms" in e.attrs:
            open_["act_end"] = e.timestamp
            open_["act_s"] = open_.get("act_s", 0.0) + float(e.attrs["ms"]) / 1000
        elif e.type == "llm.started" and open_.get("mode") is None:
            # A discovery turn ends where the model is asked for the next one.
            close("completed", e.timestamp)
        elif e.type == "step.completed":
            close("completed", e.timestamp)
        elif e.type == "step.failed":
            close("failed", e.timestamp, error_code=e.attrs.get("code"))
        elif e.type in _TERMINAL:
            # A discovery turn has no "passed" of its own; the run's end closes
            # the last one. A replay step still open here did not finish.
            close("completed" if open_.get("mode") is None else "unfinished", e.timestamp)
    return out


# -- where the time went ---------------------------------------------------------


@dataclass(frozen=True)
class _Span:
    start: float
    end: float
    bucket: Bucket


def breakdown(record: RunRecord, start: str, end: str) -> dict[str, float]:
    """Seconds per bucket, adding up to ``end - start``."""
    if not start or not end:
        return {}
    t0, t1 = parse_ts(start), parse_ts(end)
    if t1 <= t0:
        return {b: 0.0 for b in BUCKETS}
    spans = _spans(record, end)
    events = record.events
    if events and parse_ts(events[0].timestamp) > t0:
        spans.append(_Span(t0, parse_ts(events[0].timestamp), "startup"))
    points = sorted({t0, t1, *(s.start for s in spans), *(s.end for s in spans)})
    points = [p for p in points if t0 <= p <= t1]
    totals: dict[str, float] = {b: 0.0 for b in BUCKETS}
    for a, b in pairwise(points):
        mid = (a + b) / 2
        covering = [s.bucket for s in spans if s.start <= mid < s.end]
        bucket = min(covering, key=_PRIORITY.__getitem__) if covering else "other"
        totals[bucket] += b - a
    return {k: round(v, 3) for k, v in totals.items()}


def _spans(record: RunRecord, end: str) -> list[_Span]:
    spans: list[_Span] = []
    events = record.events
    for a, b, _ in _human_intervals(record, end):
        spans.append(_Span(a, b, "human"))
    starts = [e for e in events if e.type == "llm.started"]
    ends = [e for e in events if e.type == "llm.completed" and "error" not in e.attrs]
    for s, f in zip(starts, ends, strict=False):
        spans.append(_Span(parse_ts(s.timestamp), parse_ts(f.timestamp), "llm"))
    for i, e in enumerate(events):
        if e.type == "recovery.started":
            closes = (
                ("step.completed", "step.failed", *_TERMINAL)
                if e.attrs.get("action") == "retry"
                else ("recovery.completed", "recovery.exhausted", *_TERMINAL)
            )
            stop = next((x.timestamp for x in events[i + 1 :] if x.type in closes), end)
            spans.append(_Span(parse_ts(e.timestamp), parse_ts(stop), "recovery"))
    for step in _steps(events):
        t = parse_ts(step.started_at)
        located = t + step.locate_s
        acted = located + step.act_s
        spans.append(_Span(t, located, "locate"))
        spans.append(_Span(located, acted, "act"))
        spans.append(_Span(acted, t + step.duration_s, "verify"))
    # Between one step passing and the next starting: the evidence snapshot.
    for i, e in enumerate(events):
        if e.type == "step.completed":
            nxt = next(
                (x.timestamp for x in events[i + 1 :] if x.type in ("step.started", *_TERMINAL)),
                None,
            )
            if nxt is not None:
                spans.append(_Span(parse_ts(e.timestamp), parse_ts(nxt), "evidence"))
    return [s for s in spans if s.end > s.start]


def _human_intervals(record: RunRecord, end: str) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    transitions = record.transitions
    for i, t in enumerate(transitions):
        if t.to not in _HUMAN_STATES:
            continue
        stop = transitions[i + 1].ts if i + 1 < len(transitions) else end
        a, b = parse_ts(t.ts), parse_ts(stop)
        if b > a:
            out.append((a, b, t.to))
    return out


def _secs(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return round(max(0.0, parse_ts(b) - parse_ts(a)), 3)


def _secs_add(ts: str, seconds: float) -> str:
    return format_ts(parse_ts(ts) + seconds)
