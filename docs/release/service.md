# Shared-service recovery release exercise (PR17)

PR17 adds repeatable synthetic recovery checks and a reviewed evidence gate.
The shared-service release remains **unqualified**. PR13 workflow/service
envelope migration and PR15 verified browser termination/profile cleanup remain
incomplete. Passing these checks does not close those dependencies, qualify a
TLS deployment, or establish exactly-once application effects.

Use one runtime per runs directory on trusted local storage. Distributed workers
need separate ownership and session-affinity evidence. The [adoption release](adoption.md)
and its independent developer trials remain separate requirements.

## Run the automated exercise

From a reviewed checkout with `.[test]` installed, choose a new private output
directory. This command works in PowerShell and POSIX:

```text
python -m scripts.ci.run_service_exercise --out artifacts/service-recovery/candidate1
```

The runner refuses existing output directories. It runs named pytest regressions
in isolated temporary directories, records JUnit reports and local logs, and
removes temporary runtime data. It records the commit, dirty checkout state and
OS/Python environment. A dirty checkout produces development evidence but cannot
qualify a release. Use an ignored output directory such as `artifacts/` to avoid
making a clean checkout dirty through report creation.

| Exercise | Assertion and scope |
|---|---|
| Concurrency | Same identity starts one direct replay; competing consent holders dispatch at most one synthetic commit |
| Saturation | Bounded worker/queue and MCP work capacity; accepted retries resolve and overflow creates no claim |
| Operator isolation | Every route authenticates; tenant/role restrictions and exclusive assignments survive restart and process contention |
| Expired handoffs | Expired requests cannot be claimed or approved |
| Process loss | Kill subprocesses at reservation, dispatch, commit and result boundaries; independent commit oracle shows no repeat; interrupted resume/queue remains explicit |
| Shutdown | Waiting work cancels safely, active work drains, queue expiry never dispatches |
| Backup/restore | Stopped-writer copy preserves interrupted replay identity and spent consent; retention leaves retry answers unchanged |
| Retention | Tenant scope, dry-run default, active/unknown/handoff evidence protection and immutable retry authority |

These checks use synthetic adapters and a test-only external commit oracle.
The restore test copies a stopped synthetic runs directory; it does not validate
production registry/secret mounts, a real application's database, or live snapshots.
Existing strict browser/security and Windows UIA release gates remain required
for the corresponding supported surfaces.

Exit 0 means `automated_checks_passed`, with `release_status: not_qualified`.
Failures return exit 1; inspect per-exercise logs and JUnit before rerunning into
a new directory. Empty reports, missing named cases or parameter variants,
collection failures and any skips cannot pass the checker. CI runs the exercise
on Linux and Windows without uploading its reports. Keep logs/runtime material
private and review reports for application data before sharing.

## Complete the reviewed record

`record.json` contains SHA-256 references to each JUnit report, pending manual
gates and explicit dependency blockers. Preserve it with the reports. Hashes
detect file changes; they are not signatures or proof of provenance. Reviewers
must verify trusted run origin and the reviewed commit independently.

Complete these gates with dated reports in the record directory. Each report
must identify the tested commit/environment, steps, observations and reviewer:

1. `pr13_envelope_recovery`: complete outstanding workflow/service envelope work;
   exercise concurrent workflow/API attempts and process loss, including killed
   resume, with independent commit observations.
2. `pr15_session_cleanup`: complete verified browser termination/profile cleanup;
   show active sessions and reused PIDs are protected and repeated short runs
   leave no abandoned sessions. Preserve unknown outcomes and tombstones.
3. `tls_supervisor_restart`: follow [deployment](../deployment.md) with synthetic
   data behind TLS. Verify anonymous/reader refusal, operator conflict, assignment
   persistence, key rotation, readiness, shutdown and checkpoint-validated hand-back.
4. `target_application_reconciliation`: interrupt around a synthetic target write,
   compare independent receipt/commit history, retry the original key and record
   the durable answer or explicit uncertainty without a repeated effect.
5. `deployment_backup_restore`: stop **all** writers, copy complete runs, registry,
   signing configuration and secret mounts, restore into an isolated directory,
   verify seals, doctor/readiness, original retry identity and spent consent, then
   exercise scoped retention. Preserve all newer tombstones; restoring older
   authority over later writes is not a recovery procedure.

Set a manual gate's `status` to `passed` only after review and attach
`evidence: {"path": "relative-report.md", "sha256": "<64 lowercase hex characters>"}`.
Paths must resolve inside the record directory. Record unsuccessful attempts and
unresolved issues; remove an `open_blockers` entry only after its implementation
and evidence have been reviewed. Generated defaults deliberately prevent today's
implementation from qualifying through automated tests alone.

Check the complete bundle against the full reviewed commit ID:

```text
python -m scripts.ci.check_service_release artifacts/service-recovery/candidate1/record.json --commit FULL_40_CHARACTER_COMMIT
```

Exit 1 reports why the candidate is not ready. Exit 0 reports `ready_for_review`;
it does not publish a package or authorize deployment. Re-run for every changed
candidate; do not relabel old reports as evidence for a new commit.

[CI gates](../CI.md) · [Operations](../operations.md) · [Deployment](../deployment.md)
