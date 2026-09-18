"""Failure-injection registry for the mock app.

An inject mode is armed with ``?inject=<mode>`` on any request or the
``X-Inject`` header, and is stored in the session so that it fires on the
screen it belongs to rather than on the request that armed it.

Persistence matters:

* ``persistent`` modes keep firing until disarmed (``?inject=none``). They model
  states the app is genuinely in — a record that does not exist, a button that
  has been renamed — so replay should report a business outcome or a failure,
  not recover.
* ``one_shot`` modes clear themselves the first time they fire. They model
  transient conditions. If they persisted, no recovery could ever succeed and
  the recovery tests would prove nothing.

``interstitial_persistent`` is the notice that will not go away: it follows
sign-on and every return to the signed-in shell, so dismissing it only brings
it back. It exists to prove recovery is capped — a run that dismissed it
forever would never end.

``native_confirm`` is persistent for that reason and not by oversight: an
application that asks before committing an irreversible action asks *every*
time. It is not a transient condition to be waited out, it is a step the
recorded capability did not know about.
"""

from __future__ import annotations

from enum import StrEnum


class Inject(StrEnum):
    NOT_FOUND = "not_found"
    VALIDATION_ERROR = "validation_error"
    PERMISSION_DENIED = "permission_denied"
    INTERSTITIAL_DIALOG = "interstitial_dialog"
    INTERSTITIAL_PERSISTENT = "interstitial_persistent"
    SLOW_LOAD = "slow_load"
    SESSION_EXPIRED = "session_expired"
    SERVER_ERROR = "server_error"
    RENAMED_BUTTON = "renamed_button"
    AMBIGUOUS_BUTTON = "ambiguous_button"
    SLOW_CONFIRM = "slow_confirm"
    MODAL_DIALOG = "modal_dialog"
    NATIVE_CONFIRM = "native_confirm"


ONE_SHOT: frozenset[Inject] = frozenset(
    {
        Inject.INTERSTITIAL_DIALOG,
        Inject.MODAL_DIALOG,
        Inject.SLOW_LOAD,
        Inject.SESSION_EXPIRED,
        Inject.SLOW_CONFIRM,
    }
)

PERSISTENT: frozenset[Inject] = frozenset(set(Inject) - set(ONE_SHOT))

# Seconds of delay for the two timing modes. slow_load sits under the replay
# engine's step timeout so a retry can succeed; slow_confirm sits above it so
# the irreversible step times out with side_effect: unknown.
SLOW_LOAD_SECONDS = 4.0
SLOW_CONFIRM_SECONDS = 6.0

# The two dialog modes. They exist because "dialog" names two different
# problems, and only one of them is visible to a perception layer built on the
# accessibility tree.
MODAL_TEXT = "Batch posting is running. Balances may be as of last night."
"""An in-page overlay: ordinary markup, so perception sees it — but it covers
the screen, so a control resolved underneath it cannot actually be clicked."""

CONFIRM_TEXT = "Post this sub-account application now?"
"""A native ``confirm()``. It is not in the DOM, not in the accessibility tree,
and not in a screenshot of the page; it also blocks every further instruction
to the browser until something answers it. Being unable to see the thing that
is blocking you is a different failure from seeing it and being blocked."""

DISARM = "none"


def parse(value: str) -> Inject | None:
    """Return an Inject, or None for the disarm keyword.

    Raises ValueError on an unknown mode: a typo in a demo command should fail
    loudly rather than silently arm nothing and make the run look like a pass.
    """
    value = value.strip().lower()
    if value in ("", DISARM):
        return None
    return Inject(value)
