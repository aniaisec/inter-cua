"""When a discovery run stops without the agent saying so.

Three limits, each a different failure:

* ``max_steps`` — the agent is busy but not converging. Every model call counts,
  including calls whose tool input was rejected.
* ``timeout_s`` — wall clock, for a target so slow the step count never binds.
* **dead end** — the agent keeps acting and the screen does not change. Three
  looks at the same screen after three actions means it is pushing on
  something that does not move; a fourth call would cost money and change
  nothing, so the run stops before making it.

"The same screen" ignores refs, which are renumbered on every look. It is
the screen's location and content, not its numbering.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cua.surface.protocol import Observation

_REF = re.compile(r"\bn\d+\b")

StopReason = Literal["MAX_STEPS", "TIMEOUT", "DEAD_END"]


class StopLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_steps: int = 30
    timeout_s: float = 600.0
    dead_end_repeats: int = 3


def fingerprint(observation: Observation) -> str:
    """Identity of a screen, independent of how its refs were numbered."""
    frames = "|".join(f"{f.name}={f.url}" for f in observation.frames)
    tree = _REF.sub("#", observation.compact())
    dialogs = "|".join(f"{d.kind}:{d.message}" for d in observation.dialogs)
    digest = hashlib.sha256(f"{frames}\n{dialogs}\n{tree}".encode()).hexdigest()
    return digest[:16]


class Stopwatch:
    def __init__(self, limits: StopLimits) -> None:
        self.limits = limits
        self._started = time.monotonic()
        self._steps = 0
        self._last: str | None = None
        self._repeats = 0

    @property
    def steps(self) -> int:
        return self._steps

    @property
    def remaining(self) -> int:
        return max(0, self.limits.max_steps - self._steps)

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self._started

    def before_call(self) -> StopReason | None:
        """Checked before every model call; a call is only made if this is None."""
        if self._repeats >= self.limits.dead_end_repeats:
            return "DEAD_END"
        if self._steps >= self.limits.max_steps:
            return "MAX_STEPS"
        if self.elapsed_s >= self.limits.timeout_s:
            return "TIMEOUT"
        self._steps += 1
        return None

    def screen_after_action(self, observation: Observation) -> None:
        """Record the screen an action left behind.

        Only screens that follow an action that could change them count: a
        ``read`` leaves the screen alone on purpose, and three reads of three
        outputs are progress, not a dead end.
        """
        mark = fingerprint(observation)
        if mark == self._last:
            self._repeats += 1
        else:
            self._last = mark
            self._repeats = 1
