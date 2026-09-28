"""Approved capabilities as MCP tools (``cua mcp``).

An agent that speaks the Model Context Protocol connects to ``cua mcp`` over
stdio and sees one tool per approved capability,
``member_savings_balance(member_id)`` or
``open_subaccount(member_id, initial_deposit, idempotency_key)``, never a
browser primitive. A call runs through the same run service as the HTTP API
(``cua.api.service``): resolved in the registry, typed, held to the policy,
replayed with no model, and answered with the ``ReplayResult``, reduced to what
an agent acts on.

``tools`` builds the tool list and shapes results; ``server`` speaks the
protocol; ``cli`` is the command. Nothing here imports a model client.
"""
