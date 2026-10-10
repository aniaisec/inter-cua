# Windows UIA

Discover, review and replay DeskCalc in an interactive Windows session; refuse explicitly requested screenshots before starting the application.

Use an unlocked interactive Windows desktop, PowerShell/WinForms, and `python -m pip install -e ".[windows]"` from this checkout. No provider key is needed. Linux/headless/locked sessions cannot establish the live UIA gate.

Run from the repository root in PowerShell (choose a new output directory each time):

```powershell
python -m examples.run windows_uia --out artifacts/examples/windows_uia-run1
```

The command initializes an isolated Windows project, discovers `deskcalc_compute`, describes and approves its exact content as an explicitly scripted example reviewer, then executes [scenario.json](scenario.json). The runner and integration tests consume this same source. Expected contract fields are:

```json
{
  "kind": "success",
  "unsupported_refused": true,
  "replay_model_calls": 0
}
```

Success exits 0 and writes `summary.json`; any violated assertion exits nonzero. Inspect `commands.json` for the actual CLI/client calls and `demonstration.cast` for a text terminal recording of review, results and evidence paths. Replay evidence is under `project with spaces/evidence/runs/`. Discovery can have model-call records from the scripted provider; replay has none.

Each run owns its DeskCalc process, ledger and runtime files and closes the application on completion or failure. Evidence is retained for inspection. To clean up, remove only the output directory you passed after reviewing it; a later run uses a new directory and requires no target reset. Existing output directories are refused. See the [example index](../README.md) and [integration contracts](../../docs/integrations.md).
