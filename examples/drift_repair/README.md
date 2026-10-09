# Drift Repair

Refuse the original after a renamed control, propose and evaluate a draft repair, and prove that evaluation still requires review.

From this checkout, install `python -m pip install -e .` and `python -m playwright install chromium`. Use the same Python environment for the command below. No provider key or manual reset is needed.

Run from the repository root in PowerShell or POSIX (choose a new output directory each time):

```sh
python -m examples.run drift_repair --out artifacts/examples/drift_repair-run1
```

The command initializes an isolated demo project, describes and approves the demo's draft lookup as an explicitly scripted example reviewer, then executes [scenario.json](scenario.json). The runner and integration tests consume this same source. Expected contract fields are:

```json
{
  "original_refused": true,
  "evaluation_passed": true,
  "candidate_state": "draft",
  "original_unchanged": true,
  "review_required": true
}
```

Success exits 0 and writes `summary.json`; any violated assertion exits nonzero. Inspect `commands.json` for the actual CLI/client calls and `demonstration.cast` for a text terminal recording of review, results and evidence paths. Replay evidence is under `project with spaces/evidence/runs/`. Discovery can have model-call records from the scripted provider; replay has none.

[tasks.yaml](tasks.yaml) is the candidate evaluation suite for the original and renamed screens. The draft stays outside the approved registry. Inspect its rationale and evidence, then run `cua describe CANDIDATE` and `cua approve CANDIDATE --by YOUR_NAME` only after your own review. The example deliberately leaves it unapproved.

All ports are allocated dynamically. Each run owns its target, services and runtime files and reaps its processes on completion or failure. Evidence is retained for inspection. To clean up, remove only the output directory you passed after reviewing it; a later run uses a new directory and requires no target reset. Existing output directories are refused. See the [example index](../README.md) and [integration contracts](../../docs/integrations.md).
