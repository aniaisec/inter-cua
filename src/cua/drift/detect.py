"""Read a replay run for drift, and classify each drift it shows.

The signals are already in every run directory: each control a step looked
up (``locator.resolved``, with the rung it was recorded on and what every
rung found), each control that could not be named (``replay.failed`` with
``LOCATOR_UNRESOLVED`` or ``LOCATOR_AMBIGUOUS``), each checkpoint and output
that did not hold, and the screen at the moment of failure
(``observations/NNNN.json``). This module reads them through the canonical
events and turns them into ``DriftEvent`` s.

**Classification, strongest evidence first.** From the rung attempts alone:
a rung that found several controls is ``CONTROL_AMBIGUOUS``; a name rung that
found nothing while the pixel rung found exactly one control is
``CONTROL_RENAMED`` (something is still there; it answers to another name);
nothing anywhere is ``CONTROL_MISSING``. When the capability version the run
used is on disk and the failure screen was kept, both are consulted too: the
frame the ladder is scoped to may be gone, or the control may answer to its
old name in another frame (``FRAME_CHANGED``), and the control at the
recorded place must have the recorded role to count as renamed. Each event
says which of these it rests on (``basis``).

Reading only: nothing here writes to a run directory.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cua.artifact.schema import PLACEHOLDER, Capability, Checkpoint
from cua.drift.models import DriftEvent, DriftKind, RunDrift
from cua.observability.correlation import HeaderError
from cua.observability.events import Event
from cua.observability.recorder import LOG, find_runs, read_run
from cua.replay.invocation import bind
from cua.surface.conditions import LocationMatches
from cua.surface.evaluators import WebEvaluator
from cua.surface.locators import BBox, Ladder, match
from cua.surface.protocol import Node, Observation

DEFAULT_ROOTS = (Path("evidence"), Path("bench/runs"))


class Known:
    """Capability versions on disk, by name, version and content, so a run
    can be read against exactly the artifact it ran."""

    def __init__(self, capabilities: Iterable[Capability] = ()) -> None:
        self._by: dict[tuple[str, int, str], Capability] = {}
        for cap in capabilities:
            self.add(cap)

    def add(self, cap: Capability) -> None:
        self._by[(cap.name, cap.version, cap.content_hash())] = cap

    def get(self, name: str, version: int, content: str | None) -> Capability | None:
        if content is None:
            return None
        return self._by.get((name, version, content))


@dataclass(frozen=True)
class _Observed:
    seq: int
    label: str
    observation: str | None
    screenshot: str | None


def read_drift(run_dir: Path, known: Known | None = None) -> RunDrift | None:
    """None for anything but a replay run naming its capability."""
    record = read_run(run_dir)
    if record.header.kind != "replay":
        return None
    raw = _json(run_dir / "run.json")
    cap_info = raw.get("capability") or {}
    tenant = raw.get("tenant") or {}
    name, version = cap_info.get("name"), cap_info.get("version")
    if not isinstance(name, str) or not isinstance(version, int):
        return None
    corr = record.header.correlation
    cap = known.get(name, version, cap_info.get("content_sha256")) if known else None
    request = raw.get("request") or {}
    inputs = {k: str(v) for k, v in (request.get("inputs") or {}).items()}
    shots = _observed(run_dir)
    reader = _Reader(
        run_dir=run_dir,
        cap=cap,
        inputs=inputs,
        shots=shots,
        base={
            "capability": name,
            "version": version,
            "tenant_id": corr.tenant_id or "unknown",
            "app_family": str(tenant.get("app_family") or (cap.target.app_family if cap else "")),
            "run_id": corr.run_id,
            "invocation_id": corr.invocation_id,
            "injected": record.header.inject,
        },
    )
    lookups: Counter[str] = Counter()
    events: list[DriftEvent] = []
    seen: set[tuple[str, str, bool]] = set()
    for e in record.events:
        found: DriftEvent | None = None
        if e.type == "locator.resolved":
            attempts = e.attrs.get("attempts") or []
            lookups[str(e.attrs.get("recorded") or _first_rung(attempts, e.attrs.get("rung")))] += 1
            found = reader.slip(e)
        elif e.type == "locator.failed":
            lookups[_first_rung(e.attrs.get("attempts") or [], "unknown")] += 1
            found = reader.locator_failure(e)
        elif e.type == "step.failed" and e.attrs.get("code") in ("CHECKPOINT_FAILED",):
            found = reader.checkpoint_failure(e)
        elif e.type == "step.failed" and e.attrs.get("code") == "EXTRACTION_FAILED":
            found = reader.extraction_failure(e)
        if found is None:
            continue
        key = (found.step_id, found.kind, found.fatal)
        if key in seen:  # a step retried, or an output located twice
            continue
        seen.add(key)
        events.append(found)
    return RunDrift(
        run_id=corr.run_id,
        invocation_id=corr.invocation_id,
        capability=name,
        version=version,
        tenant_id=corr.tenant_id or "unknown",
        app_family=reader.base["app_family"],
        injected=record.header.inject,
        reached_app=bool(record.of("step.started")),
        lookups=dict(lookups),
        events=events,
    )


def scan(roots: Iterable[Path] = DEFAULT_ROOTS, known: Known | None = None) -> Iterator[RunDrift]:
    """Every replay run under ``roots``, read for drift. A run that cannot be
    read is skipped: ``cua metrics report`` is where unreadable runs are named."""
    for path in find_runs(list(roots)):
        try:
            drift = read_drift(path, known)
        except (HeaderError, ValueError, OSError):
            continue
        if drift is not None:
            yield drift


# -- classification -------------------------------------------------------------------


class _Reader:
    def __init__(
        self,
        *,
        run_dir: Path,
        cap: Capability | None,
        inputs: dict[str, str],
        shots: list[_Observed],
        base: dict[str, Any],
    ) -> None:
        self.run_dir = run_dir
        self.cap = cap
        self.inputs = inputs
        self.shots = shots
        self.base = base

    def _event(self, e: Event, **fields: Any) -> DriftEvent:
        return DriftEvent(**self.base, at=e.timestamp, **fields)

    def slip(self, e: Event) -> DriftEvent | None:
        """A control found, but not by the rung it was recorded on."""
        attempts: list[dict[str, Any]] = e.attrs.get("attempts") or []
        rung = str(e.attrs.get("rung"))
        recorded = e.attrs.get("recorded")
        if len(attempts) <= 1 and (recorded is None or recorded == rung):
            return None
        step = e.step_id or "?"
        first = attempts[0] if len(attempts) > 1 else {"rung": recorded, "matches": 0}
        expected = str(recorded or first.get("rung"))
        if step.startswith("outputs."):
            kind: DriftKind = "OUTPUT_CHANGED"
            why = f"the output was read by {rung}, not by {expected} as recorded"
        elif int(first.get("matches") or 0) > 1:
            kind = "CONTROL_AMBIGUOUS"
            why = f"{expected} found {first['matches']} controls; {rung} told them apart"
        elif first.get("refused") or first.get("rung") != "role_name":
            kind = "LAYOUT_CHANGED"
            why = f"{first.get('rung')} did not find it where it was recorded; {rung} did"
        else:
            kind = "CONTROL_RENAMED"
            why = f"no control answers to its recorded name any more; {rung} found it"
        return self._event(
            e,
            step_id=step,
            expected_locator_rung=expected,
            observed_rungs=_rungs(attempts),
            reason=why + " (the run went on)",
            evidence_ref=self._log_ref(e),
            kind=kind,
            fatal=False,
            resolved_rung=rung,
            basis="rung attempts logged",
        )

    def locator_failure(self, e: Event) -> DriftEvent:
        attempts: list[dict[str, Any]] = e.attrs.get("attempts") or []
        step = e.step_id or "?"
        basis = (
            "rung attempts logged"
            if e.attrs.get("attempts_from") == "log"
            else "rung attempts read from the failure message"
        )
        shot = self._shot_after(e, step)
        obs = self._observation(shot)
        ladder = self._ladder(step)
        expected = _first_rung(attempts, ladder[0].strategy if ladder else "unknown")
        kind, why = self._classify_missing(attempts, e.attrs.get("code"), ladder, obs)
        if ladder is not None and obs is not None:
            basis += ", and the failure screen read against the recorded ladder"
        return self._event(
            e,
            step_id=step,
            expected_locator_rung=expected,
            observed_rungs=_rungs(attempts),
            reason=why,
            evidence_ref=self._ref(shot) or self._log_ref(e),
            kind=kind,
            fatal=True,
            basis=basis,
        )

    def _classify_missing(
        self,
        attempts: list[dict[str, Any]],
        code: object,
        ladder: Ladder | None,
        obs: Observation | None,
    ) -> tuple[DriftKind, str]:
        several = [a for a in attempts if int(a.get("matches") or 0) > 1]
        if code == "LOCATOR_AMBIGUOUS" or several:
            a = several[0] if several else {"rung": "a rung", "matches": "several"}
            return "CONTROL_AMBIGUOUS", (
                f"{a['rung']} found {a['matches']} controls, and no rung found exactly one"
            )
        if ladder is not None and obs is not None:
            moved = _frame_change(ladder, obs)
            if moved is not None:
                return "FRAME_CHANGED", moved
        named = [a for a in attempts if a.get("rung") != "bbox"]
        pixel = next((a for a in attempts if a.get("rung") == "bbox"), None)
        if (
            named
            and all(int(a.get("matches") or 0) == 0 for a in named)
            and pixel is not None
            and int(pixel.get("matches") or 0) == 1
        ):
            if ladder is not None and obs is not None:
                node = at_recorded_place(ladder, obs)
                want = recorded_role(ladder)
                if node is None:
                    return "CONTROL_MISSING", (
                        f"no {want or 'control'} is where it was recorded on the kept screen"
                    )
                if node is not None and want is not None and node.role != want:
                    return "CONTROL_MISSING", (
                        f"a {node.role} is where the {want} was recorded; the {want} is gone"
                    )
                if node is not None:
                    was = recorded_name(ladder)
                    return "CONTROL_RENAMED", (
                        f"the {node.role} at the recorded place is now {node.name!r}"
                        + (f", recorded as {was!r}" if was else "")
                    )
            return "CONTROL_RENAMED", (
                "no rung found it by name, and exactly one control of its role is where it "
                "was recorded"
            )
        return "CONTROL_MISSING", "no rung found it, and nothing is where it was recorded"

    def checkpoint_failure(self, e: Event) -> DriftEvent:
        step = e.step_id or "?"
        shot = self._shot_after(e, step)
        obs = self._observation(shot)
        checkpoint = self._checkpoint(step, str(e.attrs.get("message") or ""))
        kind: DriftKind = "CHECKPOINT_CHANGED"
        why = f"{step} landed, but the screen no longer satisfies the checkpoint after it"
        basis = "the failure code"
        if checkpoint is not None and obs is not None:
            basis = "the failure screen read against the recorded checkpoint"
            wrong = self._wrong_location(checkpoint, obs)
            if wrong is not None:
                kind = "NAVIGATION_CHANGED"
                why = f"{step} landed on {wrong}, not where {checkpoint.id} expects"
        return self._event(
            e,
            step_id=step,
            expected_locator_rung="-",
            observed_rungs=[],
            reason=why,
            evidence_ref=self._ref(shot) or self._log_ref(e),
            kind=kind,
            fatal=True,
            basis=basis,
        )

    def extraction_failure(self, e: Event) -> DriftEvent:
        expected = str(e.attrs.get("expected") or "")
        name = expected.removeprefix("output ").split(":", 1)[0].split(" ", 1)[0]
        step = f"outputs.{name}" if expected.startswith("output ") else (e.step_id or "?")
        shot = self._shot_after(e, e.step_id or "?")
        return self._event(
            e,
            step_id=step,
            expected_locator_rung=_ladder_head(expected),
            observed_rungs=[],
            reason=f"{name} could not be read where it was recorded",
            evidence_ref=self._ref(shot) or self._log_ref(e),
            kind="OUTPUT_CHANGED",
            fatal=True,
            basis="the failure code",
        )

    # -- what the run kept -----------------------------------------------------------

    def _ladder(self, step: str) -> Ladder | None:
        if self.cap is None:
            return None
        if step.startswith("outputs."):
            spec = self.cap.outputs.get(step.removeprefix("outputs."))
            return list(spec.extract.target) if spec else None
        found = next((s for s in self.cap.steps if s.id == step), None)
        return list(found.target) if found and found.target else None

    def _checkpoint(self, step: str, message: str) -> Checkpoint | None:
        if self.cap is None:
            return None
        after = "done" if "the outputs were read" in message else step
        return next((c for c in self.cap.checkpoints if c.after_step == after), None)

    def _wrong_location(self, checkpoint: Checkpoint, obs: Observation) -> str | None:
        ev = WebEvaluator()
        for cond in checkpoint.all_of:
            if not isinstance(cond, LocationMatches):
                continue
            bound = bind(cond, self.inputs)
            if PLACEHOLDER.search(bound.pattern):
                continue  # an input it needs was not kept (a sensitive one)
            try:
                held = ev.evaluate(bound, obs)
            except (ValueError, re.error):
                continue
            if not held:
                frame = cond.within.frame if cond.within else None
                url = next((f.url for f in obs.frames if f.name == (frame or "")), obs.location)
                return url.split("?")[0]
        return None

    def _shot_after(self, e: Event, step: str) -> _Observed | None:
        """The screen stored right after the failure (``<step>.failed``, or
        ``<step>.escalated`` when it went to a person)."""
        seq = e.source_seq or 0
        return next(
            (s for s in self.shots if s.seq > seq and s.label.startswith(f"{step}.")),
            None,
        )

    def _observation(self, shot: _Observed | None) -> Observation | None:
        if shot is None or shot.observation is None:
            return None
        try:
            return Observation.model_validate_json(
                (self.run_dir / shot.observation).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None

    def _ref(self, shot: _Observed | None) -> str | None:
        if shot is None or shot.observation is None:
            return None
        return (self.run_dir / shot.observation).as_posix()

    def _log_ref(self, e: Event) -> str:
        return f"{(self.run_dir / LOG).as_posix()}#{e.source_seq}"


# -- ladders and screens --------------------------------------------------------------


def recorded_role(ladder: Ladder) -> str | None:
    roles = (getattr(r, "role", None) for r in ladder)
    return next((str(role) for role in roles if role), None)


def recorded_name(ladder: Ladder) -> str | None:
    for r in ladder:
        if r.strategy == "role_name":
            return r.name
        if r.strategy == "near_text":
            return r.text
    return None


def at_recorded_place(ladder: Ladder, obs: Observation) -> Node | None:
    """The one control the pixel rung finds on this screen, if it finds one."""
    for rung in ladder:
        if isinstance(rung, BBox):
            nodes = match(rung, obs)
            if len(nodes) == 1:
                return nodes[0]
    return None


def _frame_change(ladder: Ladder, obs: Observation) -> str | None:
    frames = {f.name for f in obs.frames}
    scoped = next((r.within.frame for r in ladder if r.within and r.within.frame), None)
    if scoped and scoped not in frames:
        return f"the {scoped!r} frame it is scoped to is not on the screen"
    for rung in ladder:
        if isinstance(rung, BBox) or rung.within is None or rung.within.frame is None:
            continue
        loose = rung.model_copy(update={"within": None})
        nodes = match(loose, obs)
        if len(nodes) == 1 and nodes[0].frame != rung.within.frame:
            where = nodes[0].frame or "top"
            return f"it answers to its recorded {rung.strategy} in the {where!r} frame instead"
    return None


def _first_rung(attempts: list[dict[str, Any]], default: object) -> str:
    return str(attempts[0].get("rung")) if attempts else str(default)


def _rungs(attempts: list[dict[str, Any]]) -> list[str]:
    out = []
    for a in attempts:
        if a.get("refused"):
            out.append(f"{a.get('rung')}=refused ({a['refused']})")
        else:
            out.append(
                f"{a.get('rung')}={int(a.get('matches') or 0)}"
                + (" untrusted" if a.get("untrusted") else "")
            )
    return out


def _ladder_head(expected: str) -> str:
    """``output x: exactly one node for table_cell "..." > bbox`` → ``table_cell``."""
    text = expected.split(" for ", 1)[1] if " for " in expected else ""
    return text.split(" ", 1)[0] or "-"


def _observed(run_dir: Path) -> list[_Observed]:
    path = run_dir / LOG
    out: list[_Observed] = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if '"observe"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("event") == "observe":
            out.append(
                _Observed(
                    seq=int(rec.get("seq") or 0),
                    label=str(rec.get("label") or ""),
                    observation=rec.get("observation"),
                    screenshot=rec.get("screenshot"),
                )
            )
    return out


def _json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.as_posix()} is not a JSON object")
    return data


def screen_of(event: DriftEvent) -> tuple[Path | None, Path | None]:
    """The observation and screenshot files an event's evidence points at."""
    if event.evidence_ref is None or "#" in event.evidence_ref:
        return None, None
    obs = Path(event.evidence_ref)
    run_dir = obs.parent.parent
    for s in _observed(run_dir):
        if s.observation and (run_dir / s.observation) == obs:
            return obs, (run_dir / s.screenshot) if s.screenshot else None
    return obs, None
