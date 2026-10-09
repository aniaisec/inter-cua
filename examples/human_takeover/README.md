# Human Takeover

Stop automation at a changed screen, let a scripted operator work on the same page, and validate the final checkpoint on hand-back.

From this checkout, install `python -m pip install -e .` and `python -m playwright install chromium`. Use the same Python environment for the command below. No provider key or manual reset is needed.

Run from the repository root in PowerShell or POSIX (choose a new output directory each time):

```sh
python -m examples.run human_takeover --out artifacts/examples/human_takeover-run1
```

The command initializes an isolated inventory project, discovers `item_lookup`, describes and approves its exact content as an explicitly scripted example reviewer, then executes [scenario.json](scenario.json). The runner and integration tests consume this same source. Expected contract fields are:

```json
{
  "automation_paused": true,
  "checkpoint_validated": true,
  "completed_steps_repeated": false,
  "kind": "success"
}
```

Success exits 0 and writes `summary.json`; any violated assertion exits nonzero. Inspect `commands.json` for the actual CLI/client calls and `demonstration.cast` for a text terminal recording of review, results and evidence paths. Replay evidence is under `project with spaces/evidence/runs/`. Discovery can have model-call records from the scripted provider; replay has none.

The operator interaction is explicitly scripted for reproducible tests. The runner uses the ordinary console endpoints to take control, changes the live page via the published CDP session, and hands it back. The hand-back must hold at `cp.done`, and no completed lookup step is replayed. `demonstration.cast`, `commands.json`, the masked intervention screenshot, human-action records and replay evidence form the short recording; the text remains usable without a video player.

[Recorded demonstration](demonstration.cast): a 23-second asciicast v2 recording
from the live October 9, 2026 scenario. It includes review, escalation, the scripted
operator action, evidence paths and the validated result. This machine's output
directory is replaced with `<example-output>`; regenerating it with the command
above preserves the local evidence paths.

All ports are allocated dynamically. Each run owns its target, services and runtime files and reaps its processes on completion or failure. Evidence is retained for inspection. To clean up, remove only the output directory you passed after reviewing it; a later run uses a new directory and requires no target reset. Existing output directories are refused. See the [example index](../README.md) and [integration contracts](../../docs/integrations.md).
