# Lookup

Call an approved read-only tool through the CLI and check integer/string outputs with zero replay model calls.

From this checkout, install `python -m pip install -e .` and `python -m playwright install chromium`. Use the same Python environment for the command below. No provider key or manual reset is needed.

Run from the repository root in PowerShell or POSIX (choose a new output directory each time):

```sh
python -m examples.run lookup --out artifacts/examples/lookup-run1
```

The command initializes an isolated inventory project, discovers `item_lookup`, describes and approves its exact content as an explicitly scripted example reviewer, then executes [scenario.json](scenario.json). The runner and integration tests consume this same source. Expected contract fields are:

```json
{
  "answers": [
    {
      "kind": "success",
      "outputs": {
        "quantity": 4,
        "item_name": "Desk lamp"
      },
      "side_effect": "none"
    }
  ],
  "commits": 0,
  "replay_model_calls": 0
}
```

Success exits 0 and writes `summary.json`; any violated assertion exits nonzero. Inspect `commands.json` for the actual CLI/client calls and `demonstration.cast` for a text terminal recording of review, results and evidence paths. Replay evidence is under `project with spaces/evidence/runs/`. Discovery can have model-call records from the scripted provider; replay has none.

All ports are allocated dynamically. Each run owns its target, services and runtime files and reaps its processes on completion or failure. Evidence is retained for inspection. To clean up, remove only the output directory you passed after reviewing it; a later run uses a new directory and requires no target reset. Existing output directories are refused. See the [example index](../README.md) and [integration contracts](../../docs/integrations.md).
