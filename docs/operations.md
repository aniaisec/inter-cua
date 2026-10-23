# Operation, consent and recovery

The service is a local, single-host runtime backed by run directories. `cua serve`
binds to loopback by default and uses access-file clients, tenants and scopes. See
[HTTP](integrations.md#http). The operator console requires separate operator
identities and tenant roles. See [deployment and access setup](deployment.md).
Keep keys, runtime files and CDP endpoints inside the single-host trust boundary.

## Start and operate

For browser takeover, configure [operator access](deployment.md#configure-an-operator),
start `cua operator` at the root and use replay with
`--handoff --headed`. The console defaults to `http://127.0.0.1:8100`. Select Take
control, repair the same live browser, then Resume or Abort. See
[handoff state and checkpoint rules](architecture.md#handoff).

With `--handoff-wait 0`, CLI returns `escalated` and a resume token;
`cua resume <token>` continues the existing run from another process. Do not start
a fresh run to replace a pending commit. Consent tokens are input-bound,
tenant-bound and single-use. Keeping the original idempotency key lets a lost
response be retrieved without repeating a write.

## Bounded admission and shutdown

Run limits apply to one service process; continue using one server per runs
directory. Both entry points accept these settings:

| Setting | Default | Meaning |
|---|---|---|
| `--workers` | 2 | Maximum simultaneous run workers |
| `--max-pending-runs` | 32 | Extra accepted reservations beyond the worker slots; zero allows only worker capacity |
| `--queue-timeout` | 300 seconds | Separate queue deadline; valid range 0.001–86400 seconds |
| `cua mcp --max-mcp-calls` | 8 | Outstanding stdio work calls; ping and immediate status polls use the reader |

For example, `cua serve --workers 2 --max-pending-runs 8 --queue-timeout 60`
accepts at most ten unfinished worker reservations. `cua mcp --workers 2 --max-pending-runs 8 --max-mcp-calls 4`
independently limits MCP work calls to four. Excess HTTP work returns `503`
and `Retry-After: 1`; excess MCP work returns a structured `server_busy` retry
hint. Preserve the original key and payload. Accepted retry identities still
resolve while full or shutting down. No per-client quota is introduced.

An accepted run is `queued` until dispatch, then `running`. Its record persists
`queued_at`, `queue_expires_at`, `started_at` and `queue_duration_ms` (queue
duration becomes available at dispatch/cancellation). A resumed run records
these fields for its latest attempt and retains the earlier escalation until
execution replaces it. The replay budget begins at dispatch, excluding queue
time. Queue expiry is checked when a run is read and immediately before
dispatch; there is no background expiry scheduler. Expired fresh work returns
`TIMEOUT` with `side_effect: none`. Cancelled/expired reservations remain counted
until their worker drains them, preventing repeated cancellation from filling
an unbounded executor backlog. A retry of the same key returns its expired
answer rather than renewing its deadline.

Graceful service shutdown stops new admission, cancels waiting first runs with
`INTERRUPTED`/no side effect, and lets active work finish under its existing
budget and ownership rules. A waiting resume that never dispatched returns to
its existing escalation with an error explanation; it is not a failed new UI
attempt. A queued record discovered after a restart is never silently
redispatched. An interrupted active write still requires reconciliation under
the ownership rules below. Stdio EOF drains the bounded accepted MCP calls;
service shutdown then cancels any remaining waiting runs and waits for active
execution. There is no forced termination of an active UI action.

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

Direct replay and resume now reserve durable invocation ownership as described
below. Workflow and HTTP/MCP envelope migration remains in progress; keep one
service per runs directory. Bounded admission is described above; the runtime
does not yet provide a retention scheduler or verified abandoned-browser cleanup.
HTTP can report `lost` when the server stopped without a persisted result.
Reconcile such runs with application state before another write.
`side_effect: unknown` also requires reconciliation; changing the key and retrying
may commit twice. Preserve the run directory, journal and original key.

### Maintenance (PR15 first increment)

Stop local services and replay writers before maintenance. Both commands print
JSON and default to a dry run, scoped to the selected tenant and project's runs
directory. Explicit `--runs-dir` follows the usual invocation-directory rules.

```text
cua maintenance inspect --tenant local --retention-days 30
cua maintenance cleanup --tenant local --retention-days 30
cua maintenance cleanup --tenant local --retention-days 30 --apply
```

This increment removes only top-level PNGs in a completed replay's `screenshots`
directory, after the result file has aged past the retention period (30 days by
default). The persisted SQLite answer must match the evidence result. Cleanup
rechecks ownership under the execution lock; busy owners are counted as failures.
Symlinks and Windows junctions are refused. Repeated cleanup is safe.

Run/result JSON, observations, logs, registry and capability provenance, legacy
runs, discovery runs, all handoff-state runs, unresolved outcomes, idempotency
answers, owner locks and spent consent remain intact. Evidence age never expires
retry safety. `.maintenance.jsonl` records intent before each deletion and totals
afterward. An interrupted cleanup may leave only an intent; rerun inspection to
see remaining files. Reports include eligible/storage bytes, expired escalations,
unknown effects and cleanup failures. Expiry is reported without changing answers.

Browser termination and abandoned-profile removal remain pending: existing PID
and profile metadata cannot establish process creation identity. Maintenance
does not terminate processes or scan/delete temporary profile directories.
Inspect these sessions manually; never kill a process merely because a saved PID
matches. This increment does not complete PR15's session lifecycle acceptance gate.

`GET /metrics` requires authenticated `read` and `operate` scopes and reports only
the caller's client, tenant and allowed capabilities. It returns queued/running/
escalated counts, queue-duration total/sample count, oldest queue age in ms,
expired intervention count, storage bytes and unknown outcomes. Values are current
persisted-record snapshots, not a background reconciliation scheduler; poll run
status for authoritative recovery. There are no key, run-id or secret labels.
Unauthenticated `/health` returns only status and version. Tenant-wide cleanup
reports remain local CLI operations, separate from client-scoped HTTP metrics.

For a consistent backup/export, stop every service, CLI, MCP and operator writer.
Copy the **whole** runs directory, including hidden `.invocations`, its format
marker, SQLite database and any journal/WAL/SHM files, owner locks, caches, spent
consent, API/workflow records and maintenance audit. Copy the registry, capability
artifacts, project/tenant/access configuration and mounted signing/credential
secrets in the same stopped-writer window, keeping secrets private. Store these
as one versioned snapshot; do not export just the database or screenshots.
Restore into an isolated directory while writers remain stopped, restore secret
mounts with their original access restrictions, and run `cua doctor` before
starting one service. Do not combine an older authority with newer evidence or
discard newer retry tombstones. Reconcile later activity before choosing a restore
point; there is no supported online backup or downgrade export command.

## Atomic files and serialized updates

Run/evidence JSON, replay answers, workflow journals/results, API run/claim
records, intervention pointers, control state, spent-consent records and waiter
heartbeats use a unique temporary file beside their destination, UTF-8/LF,
flush/fsync where supported, and atomic replacement. A failed write before
replacement leaves the previous file intact. A killed writer can leave an inert
`.tmp` sibling; readers never promote it or treat it as the current record.
File fsync does not provide a portable guarantee against every filesystem or
power failure. Parent directories are synced on supported POSIX filesystems;
Windows has no directory-fsync path in this implementation.

Cooperating read/check/write operations use bounded OS file locks and thread
locks: registry lifecycle changes, control transitions, API claim publication,
replay answer publication, spent-consent publication and JSONL appends. Workflow
journal saves compare the last read snapshot and refuse a stale update rather
than erase another attempt. Completed replay answers retain their first result,
including uncertain failures. JSONL writes are flushed/synced and serialized;
each append checks the existing log first. This favors integrity over throughput
and scans the log; retention/scalable storage remains later work.

Lock sidecars (`*.json.lock`, `*.jsonl.lock`, `*.lock.lock`) remain on disk and
are ignored by Git in the repository and starter projects. Their existence or
age does not mean a process holds the lock. The OS releases ownership when the
holder exits; never delete or steal a live lock because its timestamp is old.
Locks are for a trusted local filesystem, not a distributed/NFS coordination
protocol. Stop all old runtime/operator processes before upgrading; old writers
do not participate in the new lock protocol. An unlogged legacy control end
marker is recorded as a migration row before the next control update.

These are individual file integrity measures, not a transaction covering several
files or a browser commit. Control readers refuse snapshot/log sequence or state
mismatches. Invocation ownership adds the execution rules below; it cannot make
UI effects exactly once.

## Direct invocation ownership (PR13 first increment)

Replay and resume use `<runs>/.invocations/invocations.sqlite3` with a versioned
`format.json` marker. Short SQLite transactions publish tenant/key hashes,
request fingerprints, pinned capability version/content, run identity, phase,
side-effect status and the answer/result reference. Input values and approval
tokens are not stored in this authority; result outputs remain application data.
Evidence and capability seals keep their existing formats and locations.

An OS-held lock under `.invocations/owners/` spans execution, including resume.
A competing request receives an invocation-in-progress error; keep its original
key and retry later. No second UI session starts. Different inputs/content under
the same key are refused. Completed answers, including uncertain failures, are
immutable. A retry of an escalated invocation receives the existing resume token.
Use that token to continue the existing run. Never remove owner lock sidecars.

After a worker exits, a successor can obtain its execution lock; elapsed time
alone cannot authorize takeover. The successor reads a complete durable result
or records `INTERRUPTED`. A write dispatched without a result is conservatively
`side_effect: unknown`; it is never automatically repeated. A stale escalation
left on disk during an interrupted resume also requires reconciliation. The
database transaction never spans application interaction. Consent consumption
is serialized and persisted **before** an approved risky action; an interruption
can consume consent even when the action did not occur. Obtain replacement
consent only after reconciling the original invocation.

Before upgrading, stop all services, CLIs and operator processes, and privately
back up the **complete** runs directory, registry and signing configuration.
Existing valid cached answers are imported on first use without rewriting their
original JSON or capability seals. A matching legacy run without a cached answer
is refused for review, even when its evidence appears complete; this increment
does not guess at legacy ownership. Corrupt/ambiguous state must be preserved.
The format marker prevents a missing database from silently becoming an empty
store. Initialization/import never repairs a corrupt database automatically.

Back up and restore the store only while all writers are stopped, together with
its marker, SQLite journal if present, evidence, old caches and spent consent.
Do not downgrade a migrated runs directory: older code cannot see new ownership
or interruption tombstones, and the retained legacy cache is not a verified
export. There is no supported downgrade/export command in this increment.
Never restore an older database over later ownership records. Keep the affected
directory isolated if a consistent restore cannot preserve those records.

This is a local-filesystem, cooperating single-host protocol. Workflow steps and
HTTP/MCP workers reach the guarded replay path, but their journals/admission
envelopes are not yet migrated to this store. Cross-service polling/admission,
workflow attempt serialization, complete envelope migration and the corresponding
process-loss exercises remain PR13 work; this increment does not qualify shared
service operation. See the [implementation progress](IMPLEMENTATION_PROGRESS.md).

## Corrupt or interrupted state

Authoritative readers refuse malformed JSON, duplicate fields, non-finite JSON
numbers, invalid typed records and partial JSONL tails, including a final JSON
row without its terminating newline. Registry/control state, retry answers,
workflow recovery, intervention pointers and API records must not silently become
"missing" after corruption. HTTP and MCP report `state_corrupt` for corrupt
service state; HTTP uses 409. CLI errors identify the file and reconciliation
procedure. Read-only metrics may still show warnings and partial telemetry;
those summaries cannot authorize replay or reconstruct an approval.

1. Stop the runtime, operator console, CLI invocations and clients that might
   retry. Preserve the original idempotency keys and tenant/client scope. Do not
   choose a new key, obtain fresh consent or restart a write to bypass the error.
2. Preserve a private copy of the complete affected project/runtime state:
   run directories, retry caches, workflow journals, API claims, intervention and
   control files, spent-consent records, registry snapshots/lifecycle and review
   receipts. Keep signing keys and secret references secured with the backup.
   Record file hashes and the error path; do not overwrite the originals with a
   `.tmp` file or trim a torn audit/lifecycle tail to make it parse.
3. Reconcile every possibly affected write against the application's independent
   receipt/state/commit history. A complete log prefix cannot prove that a missing
   final event or commit did not occur. Attribute results to the original run,
   tenant, key and approval. Uncertainty keeps the invocation disabled.
4. Restore only a verified, consistent backup that preserves all later retry and
   spent-consent tombstones and approval/revocation history. A wholesale rollback
   to an older cache may enable a second commit. If the necessary records cannot
   be reconstructed and reviewed, isolate that state directory and keep execution
   stopped; there is no automatic repair or safe generic "delete the cache" step.
5. After review, verify capability seals and lifecycle history, inspect the run
   result and control/transition consistency, and run `cua doctor --tenant local`
   (substitute the affected tenant and project root). Doctor validates static
   configuration; it does not prove a write was absent. Resume execution only
   after resolving the application's state and preserving the original retry
   identity. New approvals still require the normal exact-content review gates.

No artifact schema or approval-seal migration is needed for atomic publication.
Existing valid files remain readable; ambiguous/corrupt legacy ownership is
refused instead of guessed. Keep the preserved originals for review and any
remaining PR13 envelope migration.

See [platforms](platforms.md), [testing](testing.md), [CI](CI.md) and
[troubleshooting](troubleshooting.md).

[Documentation index](index.md) · [Project README](../README.md)
