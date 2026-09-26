"""Workflows: approved capabilities composed into one typed request.

A workflow names capabilities by registry name, wires its own inputs and the
outputs of earlier steps into the inputs of later ones, and runs each step
through ``cua.replay.runner.replay`` — the same entry point as ``cua replay``,
so every step keeps its own approval gate, policy, consent, idempotency,
budget and tenant binding. Nothing here drives a browser, and nothing here
imports a model client.
"""
