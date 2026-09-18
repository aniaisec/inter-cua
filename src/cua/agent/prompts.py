"""What the discovery agent is told.

The system prompt is fixed for the whole run — goal, target, parameters,
outputs, credentials by placeholder, the allowlist and the rules — so it can be
cached. Each turn then carries only what changed: the result of the last
action and the screen it left behind.

Credentials appear only as placeholders. The model never sees a secret, and
neither does anything that records what the model said.
"""

from __future__ import annotations

from cua.agent.goal import Goal
from cua.policy.allowlist import Policy
from cua.surface.protocol import Observation
from cua.tenant import Tenant

RULES = """\
Rules:
- Act only on refs from the latest screen. Refs are renumbered on every screen; an
  old ref addresses nothing.
- Never guess. Do not invent a member id, an account, a value or a screen that is
  not in front of you. If the next step needs something you do not have, call
  `stuck` and say what is missing.
- Use the parameter values exactly as given. Type credentials only as their
  placeholders; the runner substitutes the real value.
- Take the shortest path an operator would. Do not explore menus you do not need.
- Many fields have no accessible name. They are shown as `textbox ~'Label'`, where
  the text after ~ is the label printed beside the field.
- When the goal is reached, call `done` naming the ref of every declared output
  on the current screen. The runner reads the values itself; do not type them.
- Call exactly one tool per turn, with a one-sentence reason.
"""


def system_prompt(
    goal: Goal, tenant: Tenant, policy: Policy, credential_fields: dict[str, list[str]]
) -> str:
    parts = [
        "You operate a legacy business application through its user interface, one "
        "action at a time, to discover how to accomplish a task. Every step you take is "
        "recorded and will later be replayed without you, so take steps that make sense "
        "to repeat.",
        f"Goal: {goal.goal}",
        f"Target: the {tenant.app_family} application at {tenant.base_url}.",
        _params(goal),
        _outputs(goal),
        _credentials(credential_fields),
        "Policy:\n" + policy.summary(),
        RULES,
    ]
    return "\n\n".join(p for p in parts if p)


def _params(goal: Goal) -> str:
    if not goal.params:
        return "Parameters: none."
    lines = [f"- {p.name} ({p.type}) = {p.value}" for p in goal.params]
    return "Parameters (supplied by the caller; use exactly these values):\n" + "\n".join(lines)


def _outputs(goal: Goal) -> str:
    if not goal.outputs:
        return "Outputs: none; call `done` with an empty outputs object when the goal is reached."
    lines = []
    for o in goal.outputs:
        bits = f"- {o.name} ({o.type}{', optional' if o.optional else ''})"
        lines.append(bits + (f": {o.description}" if o.description else ""))
    return "Outputs to return with `done`:\n" + "\n".join(lines)


def _credentials(fields: dict[str, list[str]]) -> str:
    if not fields:
        return ""
    lines = [
        f"- {name}: " + ", ".join(f"${{credentials.{name}.{f}}}" for f in names)
        for name, names in fields.items()
    ]
    return "Credentials (type these placeholders; never ask for or guess a value):\n" + "\n".join(
        lines
    )


def render_screen(observation: Observation) -> str:
    """The screen as the model reads it: where it is, then the compact tree."""
    lines = [f"Title: {observation.title}"]
    for frame in observation.frames:
        status = f" (HTTP {frame.status})" if frame.status else ""
        lines.append(f"Frame [{frame.name or 'top'}]: {frame.url}{status}")
    for dialog in observation.dialogs:
        lines.append(
            f"A native {dialog.kind} dialog said {dialog.message!r} and was {dialog.answer}."
        )
    lines.append("")
    lines.append(observation.compact() or "(nothing on screen)")
    return "\n".join(lines)


def kickoff(observation: Observation) -> str:
    return "Here is the first screen.\n\n" + render_screen(observation)


def after_action(result: str, observation: Observation | None, remaining: int) -> str:
    """The text of a tool result: what happened, and the screen now, if it changed."""
    lines = [result]
    if observation is not None:
        lines += ["", "Screen now:", render_screen(observation)]
    else:
        lines += ["", "The screen has not changed; its refs are still valid."]
    lines += ["", f"({remaining} actions left.)"]
    return "\n".join(lines)
