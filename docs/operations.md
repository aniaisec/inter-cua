# Operation, consent and recovery

The service is a local, single-host runtime backed by run directories. `cua serve`
binds to loopback by default and uses access-file clients, tenants and scopes. See
[HTTP](integrations.md#http). The operator console is trusted local tooling, not an
authenticated internet-facing administration console. Keep keys, runtime files and
CDP endpoints inside that trust boundary.

## Start and operate

For browser takeover, start `cua operator` at the root and use replay with
`--handoff --headed`. The console defaults to `http://127.0.0.1:8100`. Select Take
control, repair the same live browser, then Resume or Abort. See
[handoff state and checkpoint rules](architecture.md#handoff).

With `--handoff-wait 0`, CLI returns `escalated` and a resume token;
`cua resume <token>` continues the existing run from another process. Do not start
a fresh run to replace a pending commit. Consent tokens are input-bound,
tenant-bound and single-use. Keeping the original idempotency key lets a lost
response be retrieved without repeating a write.

## Evidence and metrics

A run contains `run.json`, `log.jsonl`, masked observations/screenshots where
supported, and `result.json`. Discovery records `model_calls.jsonl`; deterministic
replay has no model calls and may have no such file. Handoffs add control,
intervention, state-transition and human-action records. Failed browser runs can
keep scrubbed traces. Static preflight refusals can return without a run directory.

Use `cua metrics run <run-directory>` to explain outcomes and time allocation.
`cua metrics report`, `cua metrics humans`, `cua registry health <name>` and
`cua drift report` summarize evidence. [Observability](architecture.md#observability)
explains derived events and cost assumptions. Review evidence before sharing it;
redaction does not turn arbitrary application data into a public dataset.

## Restart, retention and uncertain outcomes

The runtime does not yet provide the durable ownership, admission queue, retention
scheduler or authenticated operator console proposed in later adoption work.
HTTP can report `lost` when the server stopped without a persisted result.
Reconcile such runs with application state before another write.
`side_effect: unknown` also requires reconciliation; changing the key and retrying
may commit twice. Preserve the run directory, journal and original key.

Manage retention manually. Do not delete evidence or caches for active handoffs,
unreconciled writes or keys callers may retry. Stop services with Ctrl+C and Abort
pending interventions before removing records. Inspect remaining application
processes after interruptions; there is no general retention or cleanup guarantee.
Preserve signing keys and registry lifecycle files; backups must keep artifacts
and approval records together.

See [platforms](platforms.md), [testing](testing.md), [CI](CI.md) and
[troubleshooting](troubleshooting.md).

[Documentation index](index.md) · [Project README](../README.md)
