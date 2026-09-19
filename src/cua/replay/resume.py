"""Resume-state search: where is this run, really?

After anything that moves the run off its own track — signing on again after
the session expired, and (M6) a human who drove the session for a while — the
engine does not take anyone's word for where it is. It looks at the screen and
tests the capability's checkpoints against it, newest first. The newest one
that holds is the point the run has provably reached; it carries on with the
step after it.

Newest first, because checkpoints nest: a screen that proves "member detail for
10003 is showing" also proves "signed on", and resuming after the older one
would redo work that is already done. A checkpoint that needs a captured output
(``cp.done``) holds only once the output has been read, so the search can end
the run there without replaying a single step.

The search never resumes *before* a step the run has already committed: an
irreversible step that happened is not re-done because an earlier screen
happens to still be showing.

The done checkpoint says only that the outputs could be read, which is true of
any screen that shows *an* answer. So it counts only together with the
checkpoint before it: after a person has driven the session, a balance read off
another member's page must not end the run as this member's.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from cua.artifact.schema import DONE, Capability, Checkpoint
from cua.replay.invocation import bind
from cua.surface.conditions import AllOf
from cua.surface.protocol import ConditionEvaluator, Observation


class ResumePoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    checkpoint: str
    next_step: int
    """Index of the first step still to run; ``len(steps)`` means none."""


def holds(
    checkpoint: Checkpoint,
    observation: Observation,
    evaluator: ConditionEvaluator,
    *,
    inputs: Mapping[str, str],
    outputs: Mapping[str, object] | None = None,
) -> bool:
    condition = bind(AllOf(all_of=list(checkpoint.all_of)), dict(inputs))
    return evaluator.evaluate(condition, observation, outputs=outputs)


def next_step_after(capability: Capability, checkpoint: Checkpoint) -> int:
    if checkpoint.after_step == DONE:
        return len(capability.steps)
    ids = [s.id for s in capability.steps]
    return ids.index(checkpoint.after_step) + 1


def find_resume_point(
    capability: Capability,
    observation: Observation,
    evaluator: ConditionEvaluator,
    *,
    inputs: Mapping[str, str],
    outputs: Mapping[str, object] | None = None,
    not_before: int = 0,
    only: str | None = None,
) -> ResumePoint | None:
    """The newest checkpoint that holds on this screen, or None.

    ``not_before``: the run must not resume at a step earlier than this — the
    step after the last irreversible step that is known to have happened.
    ``only``: consider this checkpoint alone (an operator named where to resume).
    """
    checkpoints = capability.checkpoints
    for i in reversed(range(len(checkpoints))):
        checkpoint = checkpoints[i]
        if only is not None and checkpoint.id != only:
            continue
        nxt = next_step_after(capability, checkpoint)
        if nxt < not_before:
            continue
        if not holds(checkpoint, observation, evaluator, inputs=inputs, outputs=outputs):
            continue
        if checkpoint.after_step == DONE and i > 0:
            before = checkpoints[i - 1]
            if not holds(before, observation, evaluator, inputs=inputs, outputs=outputs):
                continue
        return ResumePoint(checkpoint=checkpoint.id, next_step=nxt)
    return None
