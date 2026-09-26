"""The security benchmark: the computer-use surface treated as hostile.

Each scenario (``bench/security/scenarios.yaml``) is one attack on one threat
from ``docs/THREAT_MODEL.md``, run against the real components — the policy,
the discovery loop, replay, the token and registry checks, the browser —
and judged by its effects: what reached the attacker's origin, what the app
committed or served, where the canary password turned up, whether a run
started that should have been refused. None of it depends on a model
declining to follow an injected instruction: the "model" in these runs is a
script that follows every one.
"""
