"""Waiting for the target, by condition only.

Replay never sleeps for a fixed time hoping the screen is ready. It looks,
asks its question of what it saw, and looks again until the answer comes or the
deadline passes — so a fast screen costs one look and a slow one costs exactly
as long as it is slow. The one deliberate pause is a detector's ``backoff_s``,
which is a decision the capability made, not a guess about load time.

The clock is injected so the deadlines can be tested without waiting for them.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol, TypeVar

from cua.surface.protocol import Observation, Surface

POLL_INTERVAL_S = 0.15

T = TypeVar("T")


class Clock(Protocol):
    def now(self) -> float: ...


class MonotonicClock:
    def now(self) -> float:
        return time.monotonic()


class PausableClock:
    """A clock that stops while a human has the controls.

    Every deadline the engine holds — the step's, the invocation's budget — is
    measured on this clock, so an hour a request spends waiting for an operator
    is not an hour the run spent. Without it the first step after a handback
    would find its budget long gone and fail for time the automation never had.
    """

    def __init__(self, base: Clock | None = None) -> None:
        self._base = base or MonotonicClock()
        self._paused_at: float | None = None
        self._paused_total = 0.0

    def now(self) -> float:
        at = self._paused_at if self._paused_at is not None else self._base.now()
        return at - self._paused_total

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    @property
    def paused_s(self) -> float:
        """Time spent paused so far, the current pause included."""
        current = self._base.now() - self._paused_at if self._paused_at is not None else 0.0
        return self._paused_total + current

    def pause(self) -> None:
        if self._paused_at is None:
            self._paused_at = self._base.now()

    def resume(self) -> None:
        if self._paused_at is not None:
            self._paused_total += self._base.now() - self._paused_at
            self._paused_at = None


class Deadline:
    def __init__(self, clock: Clock, seconds: float) -> None:
        self._clock = clock
        self.at = clock.now() + seconds

    @property
    def remaining(self) -> float:
        return max(0.0, self.at - self._clock.now())

    @property
    def passed(self) -> bool:
        return self._clock.now() >= self.at


def poll(
    surface: Surface,
    deadline: Deadline,
    answer: Callable[[Observation], T | None],
    *,
    interval_s: float = POLL_INTERVAL_S,
) -> tuple[T | None, Observation]:
    """Observe until ``answer`` returns something, or the deadline passes.

    Always looks at least once, even with no time left: a step whose
    expectation already holds must not fail for want of a look.
    """
    while True:
        observation = surface.observe()
        found = answer(observation)
        if found is not None or deadline.passed:
            return found, observation
        surface.idle(min(interval_s, deadline.remaining))
