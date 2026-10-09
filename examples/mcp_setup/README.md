# MCP setup

Launch an MCP server from an unrelated directory with a verified absolute executable/project path, list approved tools and call a lookup.

From this checkout, install `python -m pip install -e .` and `python -m playwright install chromium`. Use the same Python environment for the command below. No provider key or manual reset is needed.

Run from the repository root in PowerShell or POSIX (choose a new output directory each time):

```sh
python -m examples.run mcp_setup --out artifacts/examples/mcp_setup-run1
```

The command initializes an isolated inventory project, discovers `item_lookup`, describes and approves its exact content as an explicitly scripted example reviewer, then executes [scenario.json](scenario.json). The runner and integration tests consume this same source. Expected contract fields are:

```json
{
  "unrelated_cwd": true,
  "approved_tool_listed": true,
  "kind": "success",
  "replay_model_calls": 0
}
```

Success exits 0 and writes `summary.json`; any violated assertion exits nonzero. Inspect `commands.json` for the actual CLI/client calls and `demonstration.cast` for a text terminal recording of review, results and evidence paths. Replay evidence is under `project with spaces/evidence/runs/`. Discovery can have model-call records from the scripted provider; replay has none.

[client.py](client.py) generates `mcp-config.json` with the actual absolute Python executable and arguments `-m cua.cli mcp --root PROJECT`. On Windows this is the installed environment's `Scripts/python.exe`; on POSIX it is `bin/python`. Each path is checked at runtime, including a project path containing spaces. Copy the resulting command and argument array into your MCP client's stdio configuration. Adapt only the outer client-specific keys; no client installation command is assumed. The client sends initialize/initialized, lists tools and calls only an approved capability. stdout is reserved for JSON-RPC.

All ports are allocated dynamically. Each run owns its target, services and runtime files and reaps its processes on completion or failure. Evidence is retained for inspection. To clean up, remove only the output directory you passed after reviewing it; a later run uses a new directory and requires no target reset. Existing output directories are refused. See the [example index](../README.md) and [integration contracts](../../docs/integrations.md).
