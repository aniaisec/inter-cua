"""The capability runtime over HTTP (``cua serve``).

An agent or a service invokes an approved capability with a request and gets
back the same ``ReplayResult`` ``cua replay`` prints, without starting a CLI
process per call. ``app`` builds the FastAPI application, ``access`` decides
who may call it and for what, ``service`` runs the invocations, ``models``
holds the request and response bodies, and ``routes`` the endpoints.

Like replay itself, nothing here imports a model client.
"""
