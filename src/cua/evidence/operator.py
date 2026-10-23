"""Disposable authenticated identity for explicitly scripted examples/demos."""

from __future__ import annotations

import secrets
from pathlib import Path

import yaml

IDENTITY = "evidence-bot"
KEY_ENV = "CUA_SCRIPTED_OPERATOR_KEY"


def configure(work: Path, env: dict[str, str]) -> Path:
    """Create isolated demo access; no reusable credentials are packaged."""
    path = work / "operator/access.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError("scripted operator config already exists")
    env[KEY_ENV] = secrets.token_urlsafe(32)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "operators": {
                    IDENTITY: {
                        "key": {"provider": "env", "var": KEY_ENV},
                        "tenants": {"local": ["read", "operate", "approve"]},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path
