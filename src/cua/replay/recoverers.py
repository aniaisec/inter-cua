"""Recovery bookkeeping: how many times the run may fix itself, and where.

A recoverable detector says what to do (``dismiss_notice``, ``retry_step``,
``restart_from_last_checkpoint``); this module says whether it may still be
done. Three limits, all enforced, the tightest winning:

* ``recover.max`` on the detector — this fix, at this step;
* ``recovery_limits.per_step`` — any fix, at this step;
* ``recovery_limits.per_run``, capped further by ``budget.max_recoveries``.

Without limits a notice that reappears every time it is dismissed turns a run
into a loop. Exceeding one is ``RECOVERY_EXHAUSTED``, with every recovery that
*was* made listed in the result so the pattern is visible.

Retrying a step is further gated by the step itself: only ``retry.allowed``
steps are retried, and an irreversible step never is.
"""

from __future__ import annotations

from collections import Counter

from cua.artifact.schema import Detector, RecoveryLimits, Step
from cua.replay.result import Recovery


class RecoveryLedger:
    def __init__(self, limits: RecoveryLimits, max_recoveries: int) -> None:
        self.per_step = limits.per_step
        self.per_run = min(limits.per_run, max_recoveries)
        self._by_step: Counter[str] = Counter()
        self._by_detector: Counter[tuple[str, str]] = Counter()
        self.made: list[Recovery] = []

    def refusal(self, step: Step, detector: Detector) -> str | None:
        """Why this recovery may not be made, or None if it may."""
        assert detector.recover is not None
        if detector.recover.then == "retry_step" and not step.retry.allowed:
            return f"{step.id} is not retryable" + (
                " (irreversible)" if step.risk == "irreversible" else ""
            )
        if len(self.made) >= self.per_run:
            return f"the run has used all {self.per_run} recoveries it is allowed"
        if self._by_step[step.id] >= self.per_step:
            return f"{step.id} has used all {self.per_step} recoveries a step is allowed"
        if self._by_detector[(step.id, detector.code)] >= detector.recover.max:
            return f"{detector.code} has been recovered {detector.recover.max} time(s) at {step.id}"
        return None

    def start(self, step: Step, detector: Detector, action: str) -> Recovery:
        """Count the recovery and list it as ``failed`` until ``finish`` says
        otherwise. A recovery that dies half way is then still in the result,
        which is where whoever debugs the run will look for it."""
        self._by_step[step.id] += 1
        self._by_detector[(step.id, detector.code)] += 1
        recovery = Recovery(
            step_id=step.id,
            code=detector.code,
            action=action,
            attempts=self._by_detector[(step.id, detector.code)],
            outcome="failed",
        )
        self.made.append(recovery)
        return recovery

    def finish(
        self, recovery: Recovery, *, resumed_after_checkpoint: str | None = None
    ) -> Recovery:
        done = recovery.model_copy(
            update={"outcome": "succeeded", "resumed_after_checkpoint": resumed_after_checkpoint}
        )
        self.made[self.made.index(recovery)] = done
        return done

    def counts(self) -> tuple[dict[str, int], list[tuple[str, str, int]]]:
        """The caps' counters, to carry a run on in another process."""
        return dict(self._by_step), [(s, c, n) for (s, c), n in self._by_detector.items()]

    def restore(
        self,
        made: list[Recovery],
        by_step: dict[str, int],
        by_detector: list[tuple[str, str, int]],
    ) -> None:
        self.made = list(made)
        self._by_step = Counter(by_step)
        self._by_detector = Counter({(s, c): n for s, c, n in by_detector})

    def record(
        self,
        step: Step,
        detector: Detector,
        action: str,
        *,
        resumed_after_checkpoint: str | None = None,
    ) -> Recovery:
        """A recovery with nothing to run (a plain retry): started and finished."""
        return self.finish(
            self.start(step, detector, action), resumed_after_checkpoint=resumed_after_checkpoint
        )
