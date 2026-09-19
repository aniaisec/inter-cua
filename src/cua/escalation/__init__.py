"""Handing a live session to a person, and back.

``controller``  the state machine and the per-run control record
``lease``       who may act on the session now; the surface wrapper that asks
``requests``    intervention requests and the file queue
``channel``     the engine's (and discovery loop's) handoff over those files
``capture``     what a person does while they hold the controls
``operator_app`` the console: take control, resume, retry, approve, abort
"""
