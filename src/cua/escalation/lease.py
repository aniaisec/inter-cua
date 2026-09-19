"""The control lease: one answer to "who is driving this session right now?".

Exactly one party may act on the live session at a time, and the lease names
it: the ``automation``, a ``human``, or ``none`` (paused, waiting for someone
to pick the request up). The replay process wraps its surface in
``LeasedSurface``, so every action it takes asks the lease first; an action
while a person holds it raises ``LeaseHeld`` instead of fighting the person
for the mouse.

Looking is not acting: observing the screen needs no lease, which is how the
engine can take a masked screenshot for a request, and later check what a
person left behind, without holding the controls.

The human side is not policed by the lease — a person at the browser can
always click. What the lease guarantees is that the automation never acts
over them, and the state transitions record who held it when.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cua.surface.conditions import Condition
from cua.surface.locators import Ladder, LadderOutcome
from cua.surface.protocol import (
    Action,
    ActionResult,
    ConditionEvaluator,
    ExpectDialog,
    Observation,
    SessionHandle,
    Surface,
)

Holder = Literal["automation", "human", "none"]


class ControlLease(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    holder: Holder
    since: str
    request_id: str | None = None
    """The intervention request the lease changed hands under."""


class LeaseHeld(Exception):
    """The automation tried to act while it did not hold the controls."""


class LeasedSurface:
    """A ``Surface`` that acts only while the automation holds the lease."""

    def __init__(self, inner: Surface, lease: Callable[[], ControlLease]) -> None:
        self._inner = inner
        self._lease = lease

    def _require(self, action: Action) -> None:
        lease = self._lease()
        if lease.holder != "automation":
            raise LeaseHeld(
                f"{action.action} refused: the controls are held by {lease.holder} "
                f"since {lease.since}"
                + (f" (request {lease.request_id})" if lease.request_id else "")
            )

    def act(self, action: Action) -> ActionResult:
        self._require(action)
        return self._inner.act(action)

    @property
    def evaluator(self) -> ConditionEvaluator:
        return self._inner.evaluator

    def observe(self, *, screenshot: bool = False, masks: Sequence[Ladder] = ()) -> Observation:
        return self._inner.observe(screenshot=screenshot, masks=masks)

    def resolve(self, ladder: Ladder, *, observation: Observation | None = None) -> LadderOutcome:
        return self._inner.resolve(ladder, observation=observation)

    def wait_for(self, condition: Condition, timeout_s: float) -> Observation:
        return self._inner.wait_for(condition, timeout_s)

    def evaluate(self, condition: Condition) -> bool:
        return self._inner.evaluate(condition)

    def settle(self, timeout_s: float) -> bool:
        return self._inner.settle(timeout_s)

    def idle(self, seconds: float) -> None:
        self._inner.idle(seconds)

    def expose(self) -> SessionHandle:
        return self._inner.expose()

    def expect_dialog(self, expectation: ExpectDialog) -> None:
        self._inner.expect_dialog(expectation)


def _implements_the_protocol(surface: LeasedSurface) -> Surface:
    """Static proof, checked by mypy, that the wrapper is still a Surface."""
    return surface
