"""A Surface with no browser behind it, for testing what sits above one.

It shows a fixed sequence of screens: each action that could change the page
moves to the next one, and the last one stays up. That is enough to drive the
discovery loop through every path it has — the loop only ever sees
observations, and whether they came from Chromium is not its business.
"""

from __future__ import annotations

from collections.abc import Sequence

from cua.surface.conditions import Condition
from cua.surface.locators import Ladder, LadderOutcome, resolve_ladder
from cua.surface.protocol import (
    Action,
    ActionResult,
    ExpectDialog,
    Navigate,
    Observation,
    ReadText,
    SessionHandle,
    StaleRefError,
)

PNG = b"\x89PNG fake"


class FakeSurface:
    def __init__(self, screens: Sequence[Observation]) -> None:
        self.screens = list(screens)
        self.index = 0
        self.actions: list[Action] = []
        self.observed_masks: list[Sequence[Ladder]] = []

    @property
    def current(self) -> Observation:
        return self.screens[self.index]

    def observe(self, *, screenshot: bool = False, masks: Sequence[Ladder] = ()) -> Observation:
        self.observed_masks.append(masks)
        return self.current.model_copy(update={"screenshot_png": PNG if screenshot else None})

    def act(self, action: Action) -> ActionResult:
        self.actions.append(action)
        if isinstance(action, Navigate):
            return ActionResult(action="navigate")
        ref = getattr(action, "ref", None)
        if ref is not None and self.current.find(ref) is None:
            raise StaleRefError(f"no node {ref!r}")
        if isinstance(action, ReadText):
            return ActionResult(action="read", ref=ref, text=self.current.node(ref).text)
        self.index = min(self.index + 1, len(self.screens) - 1)
        return ActionResult(action=action.action, ref=ref)

    def resolve(self, ladder: Ladder, *, observation: Observation | None = None) -> LadderOutcome:
        return resolve_ladder(ladder, observation or self.current)

    def wait_for(self, condition: Condition, timeout_s: float) -> Observation:
        raise NotImplementedError

    def evaluate(self, condition: Condition) -> bool:
        raise NotImplementedError

    def settle(self, timeout_s: float) -> bool:
        return True

    def expose(self) -> SessionHandle:
        raise NotImplementedError

    def expect_dialog(self, expectation: ExpectDialog) -> None:
        raise NotImplementedError

    @property
    def acted(self) -> list[str]:
        """The non-navigation actions, as ``tool:ref`` for easy asserting."""
        return [
            f"{a.action}:{getattr(a, 'ref', '')}" for a in self.actions if a.action != "navigate"
        ]
