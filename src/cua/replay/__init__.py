"""Deterministic replay: run an approved capability with no model in the loop.

``runner`` is the entry point (approval gate, input validation, idempotency,
credentials, browser, trace); ``engine`` walks the steps; ``detectors``,
``recoverers`` and ``resume`` are the rules it applies; ``result`` is the
four-kind contract a caller gets back.
"""
