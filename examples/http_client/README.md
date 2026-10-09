# HTTP client

Start and poll an HTTP run, reuse its request/key after retry, and refuse conflicting requests without inventing another key.

From this checkout, install `python -m pip install -e .` and `python -m playwright install chromium`. Use the same Python environment for the command below. No provider key or manual reset is needed.

Run from the repository root in PowerShell or POSIX (choose a new output directory each time):

```sh
python -m examples.run http_client --out artifacts/examples/http_client-run1
```

The command initializes an isolated inventory project, discovers `adjust_stock`, describes and approves its exact content as an explicitly scripted example reviewer, then executes [scenario.json](scenario.json). The runner and integration tests consume this same source. Expected contract fields are:

```json
{
  "state": "finished",
  "commits": 1,
  "same_run_on_retry": true,
  "conflict_refused": true
}
```

Success exits 0 and writes `summary.json`; any violated assertion exits nonzero. Inspect `commands.json` for the actual CLI/client calls and `demonstration.cast` for a text terminal recording of review, results and evidence paths. Replay evidence is under `project with spaces/evidence/runs/`. Discovery can have model-call records from the scripted provider; replay has none. Synthetic write discovery changes stock before the scenario's independent oracle baseline is captured.

[client.py](client.py) is the reusable client. Create and persist a `Submission` before sending it. `queued` and `running` are polled; `escalated` returns for explicit human action; `finished` returns for inspecting `result.kind`; `error` and `lost` return for diagnosis/reconciliation. `queued` is forward compatibility for PR14; the current server reports waiting work as `running`. An unknown state fails closed. A lost run or unknown side effect is never resubmitted automatically. Transport retries send the same body, key and request id. A 409 stops the caller; 428 means a required key was missing. 429/503 are surfaced to the caller. Keep the key on any later authorized retry.

All ports are allocated dynamically. Each run owns its target, services and runtime files and reaps its processes on completion or failure. Evidence is retained for inspection. To clean up, remove only the output directory you passed after reviewing it; a later run uses a new directory and requires no target reset. Existing output directories are refused. See the [example index](../README.md) and [integration contracts](../../docs/integrations.md).
