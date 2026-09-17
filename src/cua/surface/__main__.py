"""Print what the surface sees, for a human.

    python -m cua.surface --url http://127.0.0.1:8000/login
    python -m cua.surface --signed-in            # walk into the frameset first

This is the milestone's own verification step and it stays in the tree
afterwards, because "what does perception actually see on this screen" is the
first question to ask of every locator bug, every agent misstep and every
tenant whose app looks slightly different. It is deliberately not a ``cua``
subcommand: the CLI contract belongs to the capability workflow.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from cua.surface.playwright_surface import PlaywrightSurface
from cua.surface.protocol import Click, Navigate, TypeText


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cua.surface", description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/login")
    parser.add_argument(
        "--signed-in",
        action="store_true",
        help="sign on first, so the frameset shell is what gets dumped",
    )
    parser.add_argument("--username", default="operator")
    parser.add_argument("--password", default="operator")
    parser.add_argument("--refs", action="store_true", help="also list every node with its box")
    args = parser.parse_args(argv)

    with PlaywrightSurface.launch() as surface:
        surface.act(Navigate(url=args.url))
        if args.signed_in:
            _sign_on(surface, args.username, args.password)

        observation = surface.observe()
        print(f"# {observation.title} :: {observation.location}")
        print(observation.compact())
        if args.refs:
            print()
            for node in observation.nodes:
                print(f"{node.ref:>5}  {node.frame or 'top':<5} {node.label}  {node.bbox}")
    return 0


def _sign_on(surface: PlaywrightSurface, username: str, password: str) -> None:
    """Sign on using the same ladders a recorded capability would use."""
    from cua.surface.locators import NearText, Resolved, RoleName

    observation = surface.observe()
    for label, value in (("User ID", username), ("Password", password)):
        found = surface.resolve([NearText(text=label, role="textbox")], observation=observation)
        if not isinstance(found, Resolved):
            raise SystemExit(f"could not find the {label} field: {found.kind}")
        surface.act(TypeText(ref=found.ref, text=value))
        observation = surface.observe()

    found = surface.resolve([RoleName(role="button", name="Sign On")], observation=observation)
    if not isinstance(found, Resolved):
        raise SystemExit(f"could not find the Sign On button: {found.kind}")
    surface.act(Click(ref=found.ref))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
