# Inventory example

An independent target with semantic labels, two stock items, a reviewed stock
adjustment and a read-only state/commit-count oracle. No provider key is needed.

To run all PR 07 checks on Windows from the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/ci/test_pr07.ps1
```

This runs quality, unit, inventory browser and fresh installed-package checks.
It manages the servers and simulated operator interaction automatically. Logs
and results go under `artifacts/pr07-tests/`; see [CI options](../../docs/CI.md).

From the repository root, start:

```sh
python -m uvicorn examples.inventory.app.app:app --host 127.0.0.1 --port 8001
cua init inventory-project --template inventory
```

Follow the generated `inventory-project/README.md` for discovery, exact-content
review, approval, second-input replay, consent, sequential retry, business
outcomes and human recovery. The same guide is available in the
[packaged template](../../src/cua/resources/templates/inventory/README.md).
An installed wheel exposes the target as `inventoryapp.app:app`.

The oracle is `GET /_test/state`; inspect it independently of replay. The policy
does not allow automation to navigate there. The server has synthetic credentials
and process-local state; bind it to loopback and restart it to reset all state.
Faults are armed by the ordinary replay `--inject` flag: `renamed_control`,
`ambiguous_control`, `session_expired`, `validation_fault`, `changed_screen`.

The starter's `scenario.json` supplies discovery goals, typed parameters, outputs
and second inputs to the integration tests. Tests allocate their own ports and
temporary projects and reap the server. No capability or receipt starts approved.
