"""The allowlist and risk rules, checked before every action.

``check`` answers one of three things about an action on a screen:

* ``Allow`` — go ahead.
* ``Block`` — never, whoever asks. The action is not performed, and the loop
  that asked is told why so it can choose something else.
* ``NeedsApproval`` — permitted, but it commits something. It goes ahead only
  with an approval: a token the caller supplied, or a human. With a checked
  approval in hand, the answer is ``Allow`` naming the rule it satisfied.

Blocked, in order: an action not on the list; a screen off the allowed origins
or paths; and — for a link, whose destination is on the screen before it is
clicked — a destination off the allowed origins (``external_navigation``), off
the allowed paths, or shaped like a file to fetch (``download``).

``credential_sink`` is the same kind of check for where a credential may be
typed: only on a sign-on screen (``credential_paths``), and a secret field
(one named like ``sensitive_labels``: the password) only into a control
labelled as one. It is what stops a screen that asks to "re-enter your
password" in a form of its own, or a model told to type it into a search box,
from turning a placeholder into a leak: the value is never substituted.

It is a pure function of the action and the observation it is about, so the
same check runs in the discovery loop and in replay and can be tested without
a browser.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeAlias
from urllib.parse import urljoin, urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cua.surface.locators import Ladder
from cua.surface.protocol import Action, Click, Node, Observation, Press
from cua.tenant import Tenant

if TYPE_CHECKING:
    from cua.policy.tokens import Approval

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


BlockedKind = Literal["download", "external_navigation"]
BLOCKED_KINDS: tuple[BlockedKind, ...] = ("download", "external_navigation")


class ScrubPattern(BaseModel):
    """A shape of text that is personal data whatever it sits next to."""

    model_config = ConfigDict(frozen=True)

    id: str
    pattern: str


class Policy(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed_origins: list[str]
    allowed_paths: list[str] = Field(default_factory=lambda: [".*"])
    """Regexes searched in a URL's path. A screen, or a link's destination,
    whose path matches none of them is off limits."""
    allowed_actions: list[str] = Field(
        default_factory=lambda: ["click", "type", "press", "read", "select"]
    )
    blocked_actions: list[BlockedKind] = Field(default_factory=lambda: list(BLOCKED_KINDS))
    download_pattern: str = r"(?i)\.(csv|pdf|xlsx?|docx?|zip|exe|msi|dmg)$"
    """A link whose destination path matches this fetches a file."""
    risky_rules: list[RiskRule] = Field(default_factory=list)
    screenshot_masks: list[Ladder] = Field(default_factory=list)
    sensitive_labels: str = r"(?i)password"
    credential_paths: list[str] = Field(default_factory=lambda: [".*"])
    """Regexes searched in the path of the screen a credential is typed on:
    the sign-on screens. A credential typed anywhere else is blocked."""
    scrub_patterns: list[ScrubPattern] = Field(default_factory=list)
    """Shapes of personal data masked in everything the model or the log sees."""

    def summary(self) -> str:
        """The allowlist as the agent is told it."""
        lines = [
            f"- You may act only on {', '.join(self.allowed_origins)}.",
            f"- Allowed actions: {', '.join(self.allowed_actions)}.",
        ]
        if "external_navigation" in self.blocked_actions:
            lines.append("- Links that leave the allowed origins are blocked.")
        if "download" in self.blocked_actions:
            lines.append("- Links that download a file are blocked.")
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
    approved_rule: str | None = None
    """The risky rule an approval satisfied, when one did."""


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


def check(
    policy: Policy,
    action: Action,
    observation: Observation,
    *,
    approval: Approval | None = None,
) -> Decision:
    """``approval``: consent already checked for this invocation (see
    ``cua.policy.tokens``). It turns a risky rule's ``NeedsApproval`` into
    ``Allow``; it never lifts a ``Block``."""
    if action.action not in policy.allowed_actions:
        return Block(reason=f"action {action.action!r} is not on the allowlist")

    for frame in observation.frames:
        if not frame.url.startswith(("http://", "https://")):
            continue
        if not _allowed(policy, frame.url):
            return Block(
                reason=f"the screen is outside the allowed origins ({frame.url}); "
                "nothing may be done there"
            )
        if not _path_allowed(policy, frame.url):
            return Block(
                reason=f"the screen is outside the allowed paths ({urlsplit(frame.url).path}); "
                "nothing may be done there"
            )

    ref = getattr(action, "ref", None)
    if ref is None:
        return Allow()
    node = observation.node(ref)
    info = observation.frame_info(node.frame)
    frame_url = info.url if info else observation.location

    if isinstance(action, (Click, Press)):
        blocked = _destination_blocked(policy, node, frame_url)
        if blocked is not None:
            return blocked

    for rule in policy.risky_rules:
        matched = rule.matches(node, frame_url)
        # Enter in a field submits the form it sits in, which is the same
        # commit by another route.
        submits = (
            isinstance(action, Press)
            and action.key in SUBMIT_KEYS
            and rule.covers_location(frame_url)
        )
        if not (matched or submits):
            continue
        if approval is not None:
            return Allow(approved_rule=rule.id)
        what = node.label if matched else f"Enter on {node.label}"
        return NeedsApproval(rule=rule.id, reason=f"{what}: {rule.reason}")
    return Allow()


TEXT_ROLES: frozenset[str] = frozenset({"textbox", "searchbox"})


def credential_sink(policy: Policy, node: Node, frame_url: str, fields: list[str]) -> Block | None:
    """May these credential fields be typed into this control? ``fields`` are
    the credential field names in the text (``password``), never values."""
    if not fields:
        return None
    path = urlsplit(frame_url).path or "/"
    if node.role not in TEXT_ROLES:
        return Block(reason=f"a credential goes only into a text field, not {node.label}")
    if not any(re.search(p, path) for p in policy.credential_paths):
        return Block(
            reason=f"a credential is typed only on a sign-on screen, and {path} is not one "
            f"({node.label})"
        )
    secret = [f for f in fields if re.search(policy.sensitive_labels, f)]
    labelled = any(
        re.search(policy.sensitive_labels, t) for t in (node.name, node.near_text or "") if t
    )
    if secret and not labelled:
        return Block(
            reason=f"the secret credential field {secret[0]!r} goes only into a field labelled "
            f"as one, not {node.label}"
        )
    return None


def _destination_blocked(policy: Policy, node: Node, frame_url: str) -> Block | None:
    """A link says where it goes before it is followed; check that first."""
    href = node.attrs.get("url")
    if node.role != "link" or not href:
        return None
    target = urljoin(frame_url, href)
    if not target.startswith(("http://", "https://")):
        # javascript: and the like run in the page; where they lead is
        # checked on the screen they produce.
        return None
    if "external_navigation" in policy.blocked_actions and not _allowed(policy, target):
        return Block(
            reason=f"{node.label} leads off the allowed origins ({target}); "
            "external navigation is blocked"
        )
    if "download" in policy.blocked_actions and re.search(
        policy.download_pattern, urlsplit(target).path
    ):
        return Block(reason=f"{node.label} downloads a file ({target}); downloads are blocked")
    if _allowed(policy, target) and not _path_allowed(policy, target):
        return Block(
            reason=f"{node.label} leads outside the allowed paths ({urlsplit(target).path})"
        )
    return None


def origin_allowed(policy: Policy, url: str) -> bool:
    """Is this URL on an allowed origin? For navigation the engine does itself
    (going to a capability's entry), which is not an action anyone chose."""
    return _allowed(policy, url)


def _path_allowed(policy: Policy, url: str) -> bool:
    path = urlsplit(url).path or "/"
    return any(re.search(p, path) for p in policy.allowed_paths)


def _allowed(policy: Policy, url: str) -> bool:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    return any(
        origin == urlsplit(o).scheme + "://" + urlsplit(o).netloc for o in policy.allowed_origins
    )
