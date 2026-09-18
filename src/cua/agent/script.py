"""A discovery run as a replayable script of tool calls.

A script names controls by locator ladder, not by ref: refs are renumbered on
every look at the screen, so a ref recorded on Tuesday means nothing on
Wednesday. Each step's ladder is resolved against the screen the scripted
"model" is shown, exactly as a real model would pick a ref from it.

Every discovery run exports its own ``script.yaml``, built from the nodes the
agent actually acted on. Feeding it back with ``--llm scripted`` repeats the
run with no model and no API key, which is how a reviewer without a key sees
the loop work end to end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cua.surface.locators import Ladder

ScriptTool = Literal["click", "type", "press", "read", "done", "stuck"]


class ScriptStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: ScriptTool
    target: Ladder | None = None
    text: str | None = None
    key: str | None = None
    outputs: dict[str, Ladder] = Field(default_factory=dict)
    reason: str = ""


class Script(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    goal: str = ""
    steps: list[ScriptStep]


def load_script(path: Path) -> Script:
    return Script.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def dump_script(script: Script, path: Path) -> None:
    # Not exclude_defaults: a rung's ``strategy`` is a default, and it is the
    # discriminator the script is read back by.
    data = script.model_dump(mode="json", exclude_none=True)
    for step in data["steps"]:
        if not step.get("outputs"):
            step.pop("outputs", None)
        if not step.get("reason"):
            step.pop("reason", None)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    path.write_text(text, encoding="utf-8", newline="\n")
