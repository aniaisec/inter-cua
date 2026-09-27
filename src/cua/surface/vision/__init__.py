"""Vision as a controlled fallback, for a control the accessibility tree lost.

Where it sits in the order of things::

    role_name → near_text → table_cell      the ladder, on the tree
        ↓ every rung missed
    detector      where the recorded picture of the control is on a masked
                  screenshot: candidates, never actions   (``detector``)
        ↓
    validator     one clear match, of a click, on a safe step, clear of every
                  mask, where the tree has no other control   (``validator``)
        ↓
    policy        the same allowlist and risk rules as any action
        ↓
    execution     a click at the point, refused if the pixels there changed;
                  then the step's own expectation and checkpoint, as ever

Anything that fails a check is escalated to a person, never guessed at: a
match that is not clear, a step that commits something, a screen where a
commit rule applies. Nothing here initialises a model.

Only ``image`` and ``detector`` need the ``vision`` extra (Pillow, numpy), and
they import it when called, so the artifact schema can hold an
``Appearance`` in a build without it.
"""
