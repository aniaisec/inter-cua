"""Which outcome detectors are listening, and which one a screen triggers.

Detectors are data in the capability, not code in the engine, so a tenant whose
deployment words "not found" differently changes a string rather than Python.
The engine's part is two rules, both here:

**Scope.** A detector listens only where its ``scope`` says:

* no scope — after every step;
* ``after_step: X`` — only on the screen step X leaves behind;
* ``after_checkpoint: cp`` — on the screen left by the step that checkpoint
  follows, and every step after it.

The last is what keeps "sent back to the sign-on screen" from firing while the
run is still *on* the sign-on screen: ``SESSION_EXPIRED`` is anchored to
``cp.logged_in``, so it is deaf during ``login.username`` and
``login.password`` — where the screen is ``/login`` and the detector's own
condition is true.

**Precedence.** Among detectors that match, ``hard`` beats ``business`` beats
``recoverable``, and any of them beats the step's own ``expect_after``. A 500
page that happens to sit at the expected URL is still a 500; a "not
authorized" page with the member detail heading is still a denial.

``within`` narrows a detector to one frame, so text in the nav pane cannot
answer for the main pane.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from cua.artifact.schema import Capability, Detector, StepTimedOut
from cua.surface.protocol import ConditionEvaluator, Observation

PRECEDENCE: dict[str, int] = {"hard": 0, "business": 1, "recoverable": 2}


def listening(capability: Capability, step_index: int) -> list[Detector]:
    """The screen detectors in scope after step ``step_index``, in precedence
    order. Timeout detectors are excluded: a timeout is not a screen."""
    steps = [s.id for s in capability.steps]
    step_id = steps[step_index]
    after = {
        c.id: steps.index(c.after_step) for c in capability.checkpoints if c.after_step in steps
    }

    def in_scope(det: Detector) -> bool:
        scope = det.scope
        if scope is None:
            return True
        if scope.after_step is not None:
            return scope.after_step == step_id
        if scope.after_checkpoint is not None:
            anchor = after.get(scope.after_checkpoint)
            return anchor is not None and step_index >= anchor
        return True

    found = [d for d in capability.outcome_detectors if not isinstance(d.match, StepTimedOut)]
    return sorted((d for d in found if in_scope(d)), key=lambda d: PRECEDENCE[d.class_])


def on_timeout(capability: Capability) -> Detector | None:
    """The detector that says what to do when a step's expectation never came."""
    return next(
        (d for d in capability.outcome_detectors if isinstance(d.match, StepTimedOut)), None
    )


def first_match(
    detectors: Sequence[Detector],
    observation: Observation,
    evaluator: ConditionEvaluator,
    *,
    outputs: Mapping[str, object] | None = None,
) -> Detector | None:
    """The highest-precedence detector this screen triggers."""
    for det in detectors:
        if isinstance(det.match, StepTimedOut):
            continue
        seen = narrowed(observation, det.scope.within if det.scope else None)
        if evaluator.evaluate(det.match, seen, outputs=outputs):
            return det
    return None


def narrowed(observation: Observation, frame: str | None) -> Observation:
    """The observation as seen from one frame only."""
    if frame is None:
        return observation
    return observation.model_copy(
        update={
            "nodes": observation.in_frame(frame),
            "frames": [f for f in observation.frames if f.name == frame],
        }
    )
