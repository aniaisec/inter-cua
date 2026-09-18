"""Evaluators for condition schema.

Extracts the web-specific evaluation logic out of the condition models.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from cua.surface.a11y import normalize
from cua.surface.protocol import CONTROL_ROLES, Node, Observation, SurfaceConfig
from cua.surface.conditions import (
    SELF,
    Condition,
    Visible,
    LocationMatches,
    TextPresent,
    RegionPresent,
    ErrorBannerPresent,
    ValidationMessagePresent,
    ValueSet,
    OutputExtracted,
    AllOf,
    AnyOf,
)
from cua.surface.locators import Within

ERROR_STATUS_FROM = 500
"""Which HTTP statuses count as the app failing rather than the app answering."""

class WebEvaluator:
    """Evaluates conditions against a web accessibility tree observation."""
    
    def __init__(self, config: SurfaceConfig = SurfaceConfig()) -> None:
        self._config = config

    def evaluate(
        self,
        condition: Condition,
        observation: Observation,
        *,
        outputs: Mapping[str, object] | None = None,
        target: str | None = None,
    ) -> bool:
        """Is the condition true of this observation?"""
        if isinstance(condition, AllOf):
            return all(
                self.evaluate(c, observation, outputs=outputs, target=target) for c in condition.all_of
            )
        if isinstance(condition, AnyOf):
            return any(
                self.evaluate(c, observation, outputs=outputs, target=target) for c in condition.any_of
            )
        if isinstance(condition, Visible):
            node = self._target_node(condition.target, observation, target)
            return node is not None and node.bbox is not None and not node.bbox.empty
        if isinstance(condition, LocationMatches):
            return self._location_matches(condition, observation)
        if isinstance(condition, TextPresent):
            return self._text_present(condition.text, self._scope(observation, condition.within))
        if isinstance(condition, RegionPresent):
            wanted = normalize(condition.name)
            return any(
                n.role in ("heading", "region", "banner", "main", "form")
                and normalize(n.name) == wanted
                for n in self._scope(observation, condition.within)
            )
        if isinstance(condition, ErrorBannerPresent):
            return self._error_banner_present(condition, observation)
        if isinstance(condition, ValidationMessagePresent):
            return self._validation_message_present(condition, observation) is not None
        if isinstance(condition, ValueSet):
            node = self._target_node(condition.target, observation, target)
            if node is None or not node.value:
                return False
            return condition.value is None or normalize(node.value) == normalize(condition.value)
        return bool((outputs or {}).get(condition.name) is not None)

    def validation_message(self, condition: ValidationMessagePresent, observation: Observation) -> str | None:
        """The message text itself, for a detector that reports it as a payload."""
        node = self._validation_message_present(condition, observation)
        return node.text if node else None

    def _scope(self, observation: Observation, within: Within | None) -> list[Node]:
        if within is None or within.frame is None:
            return list(observation.nodes)
        return observation.in_frame(within.frame)

    def _target_node(self, target: str, observation: Observation, acting_on: str | None) -> Node | None:
        ref = acting_on if target == SELF else target
        return observation.find(ref) if ref else None

    def _location_matches(self, condition: LocationMatches, observation: Observation) -> bool:
        pattern = re.compile(condition.pattern)
        frames = observation.frames
        if condition.within is not None and condition.within.frame is not None:
            frames = [f for f in frames if f.name == condition.within.frame]
        urls = [f.url for f in frames]
        if condition.within is None:
            urls.append(observation.location)
        return any(pattern.search(url) for url in urls)

    def _text_present(self, text: str, nodes: list[Node]) -> bool:
        wanted = normalize(text)
        return any(wanted in normalize(n.text) for n in nodes)

    def _error_banner_present(self, condition: ErrorBannerPresent, observation: Observation) -> bool:
        frames = observation.frames
        if condition.within is not None and condition.within.frame is not None:
            frames = [f for f in frames if f.name == condition.within.frame]
        if any(f.status is not None and f.status >= ERROR_STATUS_FROM for f in frames):
            return True
        nodes = self._scope(observation, condition.within)
        if any(n.role == "alert" for n in nodes):
            return True
        return condition.text is not None and self._text_present(condition.text, nodes)

    def _validation_message_present(
        self, condition: ValidationMessagePresent, observation: Observation
    ) -> Node | None:
        nodes = self._scope(observation, condition.within)
        controls = [n for n in nodes if n.role in CONTROL_ROLES and n.bbox is not None]
        if condition.near is not None:
            wanted = normalize(condition.near)
            controls = [c for c in controls if c.near_text and normalize(c.near_text) == wanted]

        for node in nodes:
            if node.role == "alert" and node.text:
                return node
            if not node.text or node.interactive or node.bbox is None:
                continue
            if condition.text is not None and normalize(condition.text) not in normalize(node.text):
                continue
            if condition.text is None and node.role not in ("text", "paragraph", "cell"):
                continue
            if any(self._near_control(node, control) for control in controls if control.frame == node.frame):
                return node
        return None

    def _near_control(self, message: Node, control: Node) -> bool:
        assert message.bbox is not None and control.bbox is not None
        m, c = message.bbox, control.bbox
        vertical = min(abs(c.y - m.bottom), abs(m.y - c.bottom), abs(c.y - m.y))
        horizontal = min(abs(c.x - m.right), abs(m.x - c.right), abs(c.x - m.x))
        return vertical <= self._config.validation_proximity_px and horizontal <= self._config.validation_proximity_px
