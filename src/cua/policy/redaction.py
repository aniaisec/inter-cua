"""Scrub secrets out of what the model and the log are shown.

Masking the screenshot is not enough on its own. A password typed into a
field comes straight back in the accessibility tree as that field's value —
measured against the mock app's sign-on screen — so an observation is scrubbed
as well, before it is rendered for the model or written to evidence:

* every resolved credential value is replaced with ``***`` wherever it
  appears in a node's name, value or label text;
* a control labelled like a secret (``sensitive_labels``) has its value
  replaced with ``***`` whatever it holds, so a secret the run was never told
  about is covered too;
* text shaped like personal data (the policy's ``scrub_patterns``: an SSN, a
  9-16 digit account or card number) is replaced with ``***`` wherever it
  appears, whoever's it is.

The same scrub is the last thing every run log line goes through
(``RunLog.scrub_with``), so a value that reaches the log by a path nobody
thought to redact is still caught.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from cua.surface.protocol import CONTROL_ROLES, DialogEvent, Node, Observation

if TYPE_CHECKING:
    from cua.policy.allowlist import Policy

MASK = "***"


class Redactor:
    def __init__(
        self,
        secrets: Iterable[str] = (),
        *,
        sensitive_labels: str = r"(?i)password",
        patterns: Iterable[str] = (),
    ) -> None:
        # Longest first, so a secret that contains another is masked whole.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self._sensitive = re.compile(sensitive_labels)
        self._patterns = [re.compile(p) for p in patterns]

    @classmethod
    def for_policy(cls, policy: Policy, secrets: Iterable[str] = ()) -> Redactor:
        return cls(
            secrets,
            sensitive_labels=policy.sensitive_labels,
            patterns=[p.pattern for p in policy.scrub_patterns],
        )

    def text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, MASK)
        for pattern in self._patterns:
            value = pattern.sub(MASK, value)
        return value

    def observation(self, observation: Observation) -> Observation:
        nodes = [self._node(n) for n in observation.nodes]
        dialogs = [
            d.model_copy(update={"message": self.text(d.message)}) for d in observation.dialogs
        ]
        return observation.model_copy(update={"nodes": nodes, "dialogs": dialogs})

    def dialogs(self, dialogs: list[DialogEvent]) -> list[DialogEvent]:
        return [d.model_copy(update={"message": self.text(d.message)}) for d in dialogs]

    def _node(self, node: Node) -> Node:
        value = node.value
        if value and node.role in CONTROL_ROLES and self._is_sensitive(node):
            value = MASK
        elif value:
            value = self.text(value)
        update: dict[str, object] = {"value": value, "name": self.text(node.name)}
        if node.near_text:
            update["near_text"] = self.text(node.near_text)
        return node.model_copy(update=update)

    def _is_sensitive(self, node: Node) -> bool:
        return any(self._sensitive.search(t) for t in (node.name, node.near_text or "") if t)
