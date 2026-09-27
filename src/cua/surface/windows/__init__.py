"""The Windows UI Automation surface: a desktop application, driven through
the same ``Surface`` protocol as a browser.

* ``perception`` — UIA elements into the existing ``Observation`` (pure, any
  platform)
* ``uia`` — the COM and Win32 layer (Windows only)
* ``locator`` — from a node back to its live control, checked before acting
* ``actions`` — click, type, select, press, read on a desktop control
* ``conditions`` — what each condition reads on a desktop
* ``adapter`` — ``WindowsSurface`` itself

Only ``perception`` and ``conditions`` import on another platform. The rest
needs ``comtypes`` (the ``windows`` extra) and UI Automation, and nothing
outside this package imports it except where a desktop target is being run.
"""
