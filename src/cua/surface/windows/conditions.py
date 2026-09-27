"""What the condition vocabulary means on a Windows desktop.

The conditions themselves are not redefined: each is a pure function of an
observation, and a desktop observation has the same shape as a web one. What
changes is what the observation's fields hold, so each condition reads a
different fact off the same code:

========================  =====================================================
``location_matches``      ``uia://<app>/<window title>`` (the web: frame URLs)
``text_present``          names and values in the UIA tree
``region_present``        a named group, pane or heading
``visible``               an element with a non-empty box, on screen
``value_set``             a control's ``ValuePattern`` value
``validation_message``    a label near a field, by the same distance rule
``dialog_raised``         a message box (class ``#32770``) the surface answered
``error_banner_present``  text only: there is no HTTP status behind a window,
                          which is why this adapter does not claim
                          ``document_status``, and a capability relying on one
                          is refused rather than half-checked
========================  =====================================================
"""

from __future__ import annotations

from cua.surface.evaluators import WebEvaluator


class DesktopEvaluator(WebEvaluator):
    """The web evaluator's rules, over a desktop observation (see the table
    above). A subclass rather than an alias, so a desktop-only reading can be
    added here without touching the web."""
