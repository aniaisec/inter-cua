# Workflow

Bind the first lookup's typed item code output into the second lookup, then stop before that second step when the first returns a business rejection.

From this checkout, install `python -m pip install -e .` and `python -m playwright install chromium`. Use the same Python environment for the command below. No provider key or manual reset is needed.

Run from the repository root in PowerShell or POSIX (choose a new output directory each time):

```sh
python -m examples.run workflow --out artifacts/examples/workflow-run1
```

The command initializes an isolated inventory project, discovers `item_lookup` with an additional typed item-code output, describes and approves its exact content as an explicitly scripted example reviewer, then executes [scenario.json](scenario.json). The runner and integration tests consume this same source. Expected contract fields are:

```json
{
  "binding_validated": true,
  "rejected_step": "first",
  "later_step": "not_run",
  "replay_model_calls": 0
}
```

Success exits 0 and writes `summary.json`; any violated assertion exits nonzero. Inspect `commands.json` for the actual CLI/client calls and `demonstration.cast` for a text terminal recording of review, results and evidence paths. Replay evidence is under `project with spaces/evidence/runs/`. Discovery can have model-call records from the scripted provider; replay has none.

[workflow.yaml](workflow.yaml) binds `${first.output.item_code}` into the second capability input. The runner adds that string output to the editable discovery script and output declarations, using the target's Item code table row. The binding is type-checked before any action. The second lookup has no write; its absence after rejection is also asserted in the workflow journal.

All ports are allocated dynamically. Each run owns its target, services and runtime files and reaps its processes on completion or failure. Evidence is retained for inspection. To clean up, remove only the output directory you passed after reviewing it; a later run uses a new directory and requires no target reset. Existing output directories are refused. See the [example index](../README.md) and [integration contracts](../../docs/integrations.md).
