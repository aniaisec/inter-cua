"""The allowlist and risk rules, checked before every action.

``check`` answers one of three things about an action on a screen:

* ``Allow`` — go ahead.
* ``Block`` — never, whoever asks. The action is not performed, and the loop
  that asked is told why so it can choose something else.
* ``NeedsApproval`` — permitted, but it commits something. It goes ahead only
  with an approval: a token the caller supplied, or a human.

It is a pure function of the action and the observation it is about, so the
same check runs in the discovery loop and in replay and can be tested without
a browser.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, TypeAlias
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cua.surface.locators import Ladder
from cua.surface.protocol import Action, Node, Observation, Press
from cua.tenant import Tenant

SUBMIT_KEYS: frozenset[str] = frozenset({"Enter", "NumpadEnter"})


class RiskRule(BaseModel):
    """A control that commits something, by role, name and where it sits."""

    model_config = ConfigDict(frozen=True)

    id: str
    role: str | None = None
    name: str
    """Regex searched in the control's accessible name."""
    location: str | None = None
    """Regex searched in the URL of the frame holding the control."""
    reason: str

    def matches(self, node: Node, frame_url: str) -> bool:
        if self.role is not None and node.role != self.role:
            return False
        if self.location is not None and not re.search(self.location, frame_url):
            return False
        return re.search(self.name, node.name) is not None

    def covers_location(self, frame_url: str) -> bool:
        return self.location is not None and re.search(self.location, frame_url) is not None


class Policy(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed_origins: list[str]
    allowed_actions: list[str] = Field(
        default_factory=lambda: ["click", "type", "press", "read", "select"]
    )
    risky_rules: list[RiskRule] = Field(default_factory=list)
    screenshot_masks: list[Ladder] = Field(default_factory=list)
    sensitive_labels: str = r"(?i)password"

    def summary(self) -> str:
        """The allowlist as the agent is told it."""
        lines = [
            f"- You may act only on {', '.join(self.allowed_origins)}.",
            f"- Allowed actions: {', '.join(self.allowed_actions)}.",
        ]
        for rule in self.risky_rules:
            where = f" on screens matching {rule.location!r}" if rule.location else ""
            lines.append(
                f"- A {rule.role or 'control'} named like {rule.name!r}{where} {rule.reason}; "
                "it needs approval and may end the run."
            )
        return "\n".join(lines)


class Allow(BaseModel):
    model_config = ConfigDict(frozen=True)
    verdict: Literal["allow"] = "allow"


class Block(BaseModel):
    model_config = ConfigDict(frozen=True)
    verdict: Literal["block"] = "block"
    reason: str


class NeedsApproval(BaseModel):
    model_config = ConfigDict(frozen=True)
    verdict: Literal["needs_approval"] = "needs_approval"
    rule: str
    reason: str


Decision: TypeAlias = Allow | Block | NeedsApproval


def load_policy(path: Path, tenant: Tenant) -> Policy:
    text = path.read_text(encoding="utf-8").replace("{tenant.base_url}", tenant.base_url)
    return Policy.model_validate(yaml.safe_load(text))


def check(policy: Policy, action: Action, observation: Observation) -> Decision:
    if action.action not in policy.allowed_actions:
        return Block(reason=f"action {action.action!r} is not on the allowlist")

    for frame in observation.frames:
        if frame.url.startswith(("http://", "https://")) and not _allowed(policy, frame.url):
            return Block(
                reason=f"the screen is outside the allowed origins ({frame.url}); "
                "nothing may be done there"
            )

    ref = getattr(action, "ref", None)
    if ref is None:
        return Allow()
    node = observation.node(ref)
    info = observation.frame_info(node.frame)
    frame_url = info.url if info else observation.location

    for rule in policy.risky_rules:
        if rule.matches(node, frame_url):
            return NeedsApproval(rule=rule.id, reason=f"{node.label}: {rule.reason}")
        # Enter in a field submits the form it sits in, which is the same
        # commit by another route.
        if (
            isinstance(action, Press)
            and action.key in SUBMIT_KEYS
            and rule.covers_location(frame_url)
        ):
            return NeedsApproval(rule=rule.id, reason=f"Enter on {node.label}: {rule.reason}")
    return Allow()


def _allowed(policy: Policy, url: str) -> bool:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    return any(
        origin == urlsplit(o).scheme + "://" + urlsplit(o).netloc for o in policy.allowed_origins
    )
