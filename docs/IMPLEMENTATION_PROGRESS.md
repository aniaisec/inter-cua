# Implementation progress

One entry per phase of the post-v1.0 work: benchmark, observability,
registry, drift, composition, security, surfaces, and API/MCP.

## Phase 0 — Baseline

Status: COMPLETE

### Changes
- `docs/baseline/TEST_BASELINE.md`: environment, gate results, and demo metrics.
- No production code changed.

### Tests
- Full gate: ruff, `ruff format --check`, `mypy --strict`, pytest.

### Commands
- The `make test` target's commands, run one by one (`make` is not installed on the Windows dev machine).
- README demo path steps 1–3 (live and scripted discovery, approve, replay) against the mock app.

### Results
- 443 passed (364 without a browser, 79 with one) in 340–401 s. Nothing failed or was skipped.
- Live discovery: 6 model calls, 36,565 input and 240 output tokens, 22.4 s.
- Replay: 11/11 successes, median 5.29 s, 5 actions, 0 model calls.

### Known issues
- None in the code. Run CLI commands from PowerShell: Git Bash rewrites `--entry /login`.

### Commit
- `chore: establish inter-cua baseline`, tag `inter-cua-baseline`.

### Next
- Phase 1: benchmark harness.

## Phase 1 — Benchmark harness

Status: COMPLETE

### Changes
- `src/cua/benchmark/`: task and metric models, suite loader, price table, scoring, both runners, the session runner, JSONL storage, aggregation and the markdown/JSON report.
- `cua benchmark list | run | report`; `make benchmark`.
- `bench/tasks/core.yaml`: 15 unattended tasks and 1 manual one (handoff/resume). Each task states its ground truth independently of either strategy.
- `bench/scripts/`: templated scripted-model runs. `bench/pricing.yaml`: prices with source and date.
- `mockapp`: `/_debug/stats` counts commits across all sessions, so a duplicate commit made by a second browser is seen.
- The replay side of the benchmark imports no model client and no `cua.agent`, and neither does the CLI parser; a test enforces both.

### Tests
- `tests/unit/test_benchmark.py`: suite, scoring, commits, prices, usage normalisation, aggregation, storage, purity.
- `tests/integration/test_benchmark.py`: a scripted session end to end (13 runs). Replay makes no model calls, the baseline's retried commit counts as a duplicate, and replay's is served from the idempotency cache.

### Commands
- `cua benchmark run --suite core --repetitions 10`: scripted model, harness check.
- `cua benchmark run --suite core --repetitions 3 --llm gemini`: live model, the comparison.
- `cua benchmark report --session bench_01M388TQM8J5S38B4HTPFXZ8V5`: `bench/reports/summary.md`.

### Results (live session `bench_01M388TQM8J5S38B4HTPFXZ8V5`, gemini-3.8-flash, 92 runs, 30 min, ~$2.49 est.)
- Baseline (model every time): 38/45 exact (84%, 95% CI 71-92), 5 safe stops, 2 wrong.
  - 8.0 model calls, 68k tokens and ~$0.053 per run; median 23.4 s, p95 48.2 s.
- Replay: 36/45 exact (80%, 95% CI 66-89), 9 safe stops, 0 wrong.
  - No model calls and no model cost; median 10.0 s, p95 23.7 s.
- Discovery: ~$0.051 per capability, about one baseline run. The model-cost break-even is therefore at the first invocation.
- Where they differ (3 runs each, so these are observations, not rates):
  - Drift (renamed button): the model adapts; replay stops safely (`LOCATOR_UNRESOLVED`).
  - Slow confirm: the model waits and reads the reference; replay reports `side_effect: unknown` and stops.
  - Retried request: the model opened a second and third sub-account (2 duplicate commits); replay committed once and served the retries from its idempotency cache.
  - Validation error: the model looped into a dead end in 2 of 3 runs; replay returned `VALIDATION_ERROR` every time.
  - Persistent notice: both stop.
- The judge was checked by hand: every baseline stop it scored as a business answer names the outcome ("No matching member", "not authorized to view this record").

### Results (scripted session `bench_01M3829XWS78E1CSF0DD9H8CHM`, 302 runs, 37 min)
- Replay: 120/150 exact, 30 safe stops, 0 wrong, 0 model calls, 0 duplicate commits.
  - Safe stops: renamed button (`LOCATOR_UNRESOLVED`), persistent notice (`RECOVERY_EXHAUSTED`), slow confirm (`TIMEOUT`, `side_effect: unknown`, one commit).
- Scripted baseline: 9 duplicate commits on the retried request, and no business-outcome answers.
  - This is a played-back script, not a model. It validates the harness, not the comparison.
- Replay latency: median 6.6 s on the clean lookup. The scripted loop takes 2.4 s on the same task, so replay's own overhead (tracing, screenshots, checkpoint waits) is not negligible. With a live model, discovery took 22 s for the same task in the Phase 0 baseline.

### Known issues
- The baseline has no typed channel for business outcomes. Its stop reason is matched against a per-task regex (a declared heuristic).
- The handoff/resume task needs a person and is listed, not run.
- Replay runs in this session were about 1.5x slower than the Phase 0 CLI measurement. The CLI measured the same on the same machine at the time, so the cause is machine load, not the harness.
- Three live repetitions per task give wide intervals. More repetitions (and a Claude run) would tighten the comparison, and cost money.
- Under heavy machine load, one existing browser test (`test_operator_can_attach_to_the_live_session_over_cdp`, fixed 10 s wait) timed out once in a full gate. It passed 3/3 when rerun alone.

### Commit
- `feat: add computer-use benchmark harness`.

### Next
- Phase 2: observability and cost accounting. Replay's own latency (about 1 s per step in checkpoint and settle waits) is the first thing worth explaining.

## Phase 2 — Observability and cost accounting

Status: COMPLETE

### Changes
- `src/cua/observability/`: `events.py` (the canonical vocabulary: `run.*`, `step.*`, `llm.*`, `locator.*`, `recovery.*`, `policy.*`, `human.*`, `side_effect.*`), `correlation.py` (run, invocation, tenant, capability id/version from `run.json`), `recorder.py` (every run file read into canonical events, with the mapping from each legacy log name in one table), `metrics.py` (one run explained, with a time breakdown that adds up to the wall clock), `health.py` (per-capability, per-version health), `cost.py` (the price table and usage normalisation, moved from the benchmark so both share them), `cli.py`.
- `cua metrics run <run_id|dir> [--json|--events]`, `cua metrics capability <name> [--version N]`, `cua metrics report [--out DIR]`.
- `cua benchmark report` adds "Where the time goes" per strategy when the run directories are on disk; `bench/reports/summary.*` regenerated for the live session with it.
- The discovery loop now times each model call (`ms` in `model_calls.jsonl`). Older runs get an inferred duration, flagged as such.

### Decisions
- Canonical events are derived from the run directory, not written beside the log. The log stays the one record: old evidence reads exactly like new, and there is no second writer to drift from the first. Each event names its source file and line (`source`, `source_seq`), and anything inferred rather than read is marked `derived` with its basis.
- `invocation_id` is `idem:<key>` when the request carried an idempotency key, else the run id: a retried request is one invocation across its runs.
- Events carry only the fields a question needs: no typed text, no observation trees, no URL query strings (a test plants sentinels to check).
- A business outcome (NOT_FOUND, PERMISSION_DENIED) is `run.completed`: the capability worked.
- Health leaves out runs with an injected fault (they measure the fault) and runs refused for want of an approval (the policy protecting the caller). Both are counted beside the rates, not hidden.
- Cost is priced per call; a model with no price is *unpriced*, never free. Every figure is labelled an estimate.

### Tests
- `tests/unit/test_observability.py`: every committed evidence run reads into valid, correlated, ordered events whose time breakdown sums to the wall clock; a clean replay, a live discovery (tokens and cost match `model_calls.jsonl` and the price table), a handoff (human wait, actions, commit); built run dirs for drift, locator failure, recovery, a torn line, measured vs inferred model waits, unpriced and scripted models, no leaked values; health rates and exclusions; the CLI.
- The purity test now also holds `cua.observability` to "imports no model client".

### Results
- All 520 run directories on this machine (evidence/ and bench/runs/) read without error; the time breakdown sums to the wall clock for every one; `cua metrics report` over them takes about 5 s.
- Where replay's time goes (live session, 43 runs, mean 11.05 s): verify 3.85 s, evidence 3.19 s, locate 2.59 s, startup 0.59 s, act 0.32 s, recovery 0.23 s. Storing the per-step observation and screenshot is ~29% of a replay; the browser actions themselves ~3%.
- Baseline (45 runs, 25.07 s): model waits 21.24 s (85%).
- Two old discovery runs used gemini-3.6/3.7-flash, which have no price entry; they are reported unpriced.

### Known issues
- For discovery, the `verify` bucket is the loop settling and observing the page after an action (the same work as replay's verify, without a checkpoint).
- `recovery.retry` has no end event of its own; its span ends when the step passes or fails.

### Commit
- `feat: add run observability and cost metrics`.

### Next
- Phase 3: capability registry. Replay's evidence snapshot and locate time are the obvious latency targets.

## Phase 3 — Capability registry

Status: COMPLETE

### Changes
- `src/cua/registry/`: `models.py` (the ledger entry and the version record: name, version, status, app family, tenant scope, artifact hash, approval, history, health), `lifecycle.py` (the statuses and the transitions allowed between them), `store.py` (the registry on disk: working copies, registered copies, the ledger), `resolver.py` (which version a call runs, and the start and in-flight policy), `health.py` (per-version health from the replays on record), `cli.py`.
- `cua registry list | versions | show | health | deprecate | revoke | reinstate | sync`.
- `cua approve` registers what it approves: a sealed copy under `capabilities/registry/<name>/v<N>.json` and a line in `capabilities/registry/lifecycle.jsonl`. It refuses to re-approve a deprecated (use `reinstate`) or revoked (final) version.
- `cua catalog` is a view of the registry: one entry per capability, the version a call by name runs. `cua catalog invoke --version N` pins a version.
- `cua replay` and `cua catalog invoke` refuse a revoked version (`POLICY_BLOCKED`, no browser) and warn on a deprecated one (`policy.deprecated` in the log, mapped to `policy.checked`); `run.json` records the version's lifecycle status. `cua resume` refuses to carry on a paused run of a revoked version.
- The two committed capabilities are registered (`cua registry sync`).

### Decisions
- The artifact schema is unchanged (1.1). `approval_state` stays `draft | approved`, sealed into the file by `cua approve`; deprecation and revocation are about a version's standing, not its content, so they live in an append-only ledger beside it. Status = the artifact's approval with the ledger applied. The ledger fails closed: a revocation applies to the name and version whatever file claims to be it, a ledger entry can never approve what the artifact does not, and an unreadable ledger refuses every run.
- `review` is the existing receipt from `cua describe` (content and version as described on this machine); `draft → review → approved` is the existing gate. The registry owns the transitions after it.
- `deprecated → revoked` is allowed beyond the listed transitions: revoking a superseded version must not require reinstating it first, which would make it the default for a moment. `revoked` is terminal.
- Working copies stay at `capabilities/<name>.json`, so every existing path and command keeps working. A registered copy is never changed; one edited by hand is skipped and reported, and registering different content under an existing version is refused.
- By name, the highest approved version runs. With nothing approved, the highest version is returned so that replay refuses it with a typed result rather than a usage error.
- In-flight policy: the lifecycle is checked when an execution starts. A run under way when its version is revoked finishes (stopping between a commit and its confirmation turns a known outcome into `side_effect: unknown`); an idempotent retry is answered from the cache before the check, because a stored result starts nothing; a paused run is an execution waiting to start again, so `cua resume` refuses it and the operator can abort it on the console.
- Tenant scope is the tenants whose deployment runs the capability's app family (`tenants/*.yaml`); restricting a version to some of them is left to tenant isolation.
- Health is only observed: a version with no replay on record has none. Runs of versions no longer on disk are still shown (`not on disk`).

### Tests
- `tests/unit/test_registry.py`: identity; sync; versions coexisting after a re-record (the approved one still runs from its registered copy, and becomes the non-default once the next is approved); registered copies never replaced or trusted after a hand edit; the transition table; the ledger never approving; deprecate, reinstate and revoke with who and why; revoke needs a reason; drafts and review; approve refusing retired versions; the default skipping deprecated and revoked; a revoked version starting nothing by working copy or registered path; a cached retry still answered; an unreadable ledger failing closed; catalog and registry agreeing; `catalog invoke --version`; health from the committed evidence; every CLI command; the committed capabilities registered and approved.
- The purity test also holds `cua.registry` and `cua.catalog` to "imports no model client".

### Results
- Gate: ruff, `ruff format --check`, `mypy --strict` clean; 542 tests pass (461 without a browser, 81 with one; 509 before this phase), about 9 minutes.
- `cua registry list` on the repository: member_savings_balance v3 and open_subaccount v3, approved and registered.
- `cua registry show open_subaccount`: 28 replays on record, 100% success, 25% with a person (the handoff evidence), p95 29.8 s.
- `cua registry health member_savings_balance`: v1 (22 replays) and v2 (4, 3 of them `AUTH_FAILED`) are not on disk; v3 has 50 replays at 100%, with 100 injected-fault runs left out.

### Known issues
- Versions before the registry existed (member_savings_balance v1 and v2) are not on disk; their runs are shown in `cua registry health` as `not on disk`.
- `cua registry show` and `health` read every run directory's `run.json` to find the capability's replays (~5 s over the ~520 runs on this machine).

### Commit
- `feat: add capability registry and lifecycle`.

### Next
- Phase 4: capability drift detection and evolution.

## Phase 4 — Capability drift and candidate evolution

Status: COMPLETE

### Changes
- `src/cua/drift/`: `models.py` (the `DriftEvent`, the eight drift kinds, the candidate record), `detect.py` (a replay run directory read for drift and classified), `aggregate.py` (drift rates), `candidate.py` (a candidate repair proposed from a run's evidence), `checks.py` (the candidate's static security checks), `evaluate.py` (the candidate replayed beside the version it repairs on the benchmark tasks), `store.py` (candidates on disk, their status, the approval gate), `rationale.md` rendering, `cli.py`.
- `cua drift scan | report | propose | evaluate | candidates | show | reject`.
- The replay engine logs what each rung found (`attempts`) on a locator failure, as data. The recorder carries rung attempts on `locator.resolved` and `locator.failed`, reads them back out of the message for older runs, and now counts `LOCATOR_AMBIGUOUS` as a locator failure too.
- `cua approve` accepts a candidate only after its evaluation passed on exactly its content, and never after it was rejected; the ledger notes it as a repair of the version it came from. `Registry.for_path` knows the candidates directory.
- `cua.benchmark.replay_runner.run_replay` takes `allow_draft`, for evaluating a candidate.

### Decisions
- Drift events are derived from run directories, like the canonical events: nothing new is written during a run, and old evidence is classified like new. Each event says what its classification rests on: the logged attempts, the attempts read from the failure message (older runs), and the failure screen read against the recorded ladder when that version is on disk.
- `version` is an int, as everywhere else in the registry (the plan sketched a string).
- Classification: several matches is `CONTROL_AMBIGUOUS`; the scoped frame gone, or the control answering to its recorded rung in another frame, is `FRAME_CHANGED`; the name rung finding nothing while the pixel rung finds exactly one control of the recorded role is `CONTROL_RENAMED`; nothing there is `CONTROL_MISSING`. A run that survived on a weaker rung is non-fatal drift (`CONTROL_RENAMED` from a name rung, `LAYOUT_CHANGED` from a label or grid rung, `OUTPUT_CHANGED` for an output). `CHECKPOINT_FAILED` is `NAVIGATION_CHANGED` when the checkpoint's location condition fails on the kept screen, else `CHECKPOINT_CHANGED`; `EXTRACTION_FAILED` is `OUTPUT_CHANGED`.
- Rates: drift events per invocation (a retried request is one invocation), counting only runs that reached the app; by rung, per lookup recorded on that rung. Injected drift is counted and shown in its own column (`--exclude-injected` leaves it out). Registry health's `drift_rate` is unchanged: it is still runs that survived a slip; failures stay under `locator_failure_rate`.
- Only `CONTROL_RENAMED` is repaired, and without a model: the kept failure screen shows the same control at the recorded place, and the repair names it with `ladder_for` in front of the recorded rungs, which stay, so a tenant still on the old screen is served (with a slip warning). Other kinds are reported with a pointer to re-record.
- Candidates live in `capabilities/candidates/<name>/v<N>/` (`capability.json`, `candidate.json`, `rationale.md`, `evidence/`), numbered after every version and candidate of the name, and are drafts. The registry does not read them, the catalog does not offer them, and nothing else under `capabilities/` changes. The same repair proposed twice returns the first.
- Security results are static checks (only that step's ladder differs; the new ladder names the same control on the failure screen by a trusted rung; recorded rungs kept; no new text the policy scrubs; a committing step flagged `attention`; draft and unregistered) plus the benchmark's `security` tasks. The prompt-injection benchmark is Phase 6.
- Evaluation gates: static checks, at least one task ran, the task injecting the same fault is answered exactly (a drift that was not injected has no such task; the gate says so), no task worse than the incumbent, no wrong answer or unexpected commit, security tasks exact. A subset run (`--task`) is recorded as such.
- Approval stays a person's: `cua describe` then `cua approve` on the candidate path, refused until the evaluation passed on that content; rejection is final for that candidate.
- No candidate is committed: the only drift on record is injected, and a repair of it would be rejected on review.

### Tests
- `tests/unit/test_drift.py`: the committed renamed-button run is `CONTROL_RENAMED` (from the screen with the version on disk, from the rungs alone without it); the failure message read back into attempts; ambiguous, missing, frame gone, something else at the recorded place, navigation and checkpoint changes, output drift, drift a run survived (deduplicated per step), clean, refused and discovery runs; rates by capability, tenant and rung, with retries as one invocation and unreached runs left out; every committed run scans; a candidate proposed with production byte for byte unchanged, v3 still the default, the repair naming Find on the failure screen and Search on the old one, and a second proposal returning the first; refusals (no drift, not repairable, version not on disk); checks failing a candidate that changes more than a ladder, names another control, carries a sensitive label or reuses a registered number; approval refused before evaluation, after a failed one, for other content, after rejection and after a hand edit, then accepted, registered as v4 and made the default with v3 still approved and the working copy untouched; the gates; the tasks for a capability; every CLI command.
- `tests/integration/test_drift.py`: v3 replayed with `renamed_button` against the mock app fails `LOCATOR_UNRESOLVED` with its attempts logged as data, is classified, proposed as v4 and evaluated (v3 0/1 and v4 1/1 on the drift task, v4 1/1 on the clean one); nothing outside `candidates/` changed and v3 is still the default.
- The purity test also holds `cua.drift` to "imports no model client".

### Results
- Gate: ruff, `ruff format --check`, `mypy --strict` clean; 565 tests pass (542 before this phase), about 9 minutes.
- `cua drift report` over the 273 replay runs on this machine: 24 drift events, all `CONTROL_RENAMED` at `search.submit`, all injected (`renamed_button`), all fatal: 8.8% per invocation overall; member_savings_balance v3 20/151 (13.2%); per rung, role_name 24/800 lookups (3.0%), near_text 0/1056, table_cell 0/170. Reading them takes about 1 s.
- Candidate v4 from the committed handoff evidence, evaluated on all 10 member_savings_balance tasks (1 repetition each): identical to v3 on 9, and on `lookup-renamed-button` v3 stops (`LOCATOR_UNRESOLVED`) while v4 answers exactly. All gates pass; about 3.5 minutes. The candidate on the old screen resolves by its second rung, with a slip warning.

### Known issues
- Only `CONTROL_RENAMED` has an automatic proposal. The rest need a person or a new discovery run.
- A drift that was not injected has no benchmark task reproducing it, so `repairs_the_drift` rests on the failure screen alone (the gate says so).
- Tasks that need consent run with consent minted for the evaluation's own mock app, as in the benchmark; `open_subaccount` has no drift on record to try it on.
- Running replay or evaluation from a thread that already holds a sync Playwright fails; the integration test runs them on a worker thread, as a CLI process would be.

### Commit
- `feat: add capability drift tracking and candidate evolution`.

### Next
- Phase 5: capability composition.

## Phase 5 — Capability composition

Status: COMPLETE

### Changes
- `src/cua/workflow/`: `models.py` (the workflow file, bindings, `WorkflowResult`), `planner.py` (each step resolved in the registry; step inputs from workflow inputs and earlier outputs), `validator.py` (static checks and per-tenant refusals), `journal.py` (workflow-level idempotency by key), `runner.py`, `cli.py`.
- `cua workflow list | check | run | approval-token`.
- `workflows/open_member_subaccount.yaml`: `member_savings_balance` then `open_subaccount`, with outputs from both.
- `cua.replay.invocation.check_input` (was private `_check`): one value against its declared type and pattern, shared with workflow inputs and literals.

### Decisions
- Every step runs through `cua.replay.runner.replay`, unchanged. The workflow adds wiring and ordering, and never goes around a child's approval gate, policy, lifecycle check, consent, budget or idempotency.
- Bindings are exactly `${input}`, `${step.output.name}` or a literal, never a template, so every one is type-checked before the run. A value flows only into an input of its own type (or integer into decimal). An optional value never feeds a required input. A sensitive value is bound only to a sensitive input, and a workflow input declared sensitive is never returned as an output. An unused workflow input is an error.
- A wrong definition is a usage error (exit 64). A step that is a draft, revoked, for another app family or on an unsupported surface refuses the whole workflow (`POLICY_BLOCKED`) before any step, so an earlier step never runs for nothing.
- The first step that does not succeed ends the workflow and gives it its kind and code; later steps are `not_run`. `side_effect` folds as unknown > committed > none.
- Consent is per committing step (`--approval <step>=<token>`). A token whose step inputs are known up front is verified before the first step (signature, capability, content, tenant, inputs, unspent). Without a token and without `--handoff`, the workflow is refused before the lookup. A step whose inputs come from an earlier output has its token checked by its own replay. `cua workflow approval-token` refuses that case and points to `--handoff`.
- A workflow with a committing step requires an idempotency key. Each step runs under `wf:<workflow>:<key>:<step>`, so retries are answered by replay's own cache. The journal (`<runs>/.workflows/`) keeps the request fingerprint, the resolved versions (a retry runs the same plan), each step's last state and run dir, and the final result.
- An escalated step is never started again: after `cua resume`, re-running with the same key reads its answer from its run dir and carries on. A committing step left `started` (process killed) is looked up by its key; its written result is the answer, else `INTERRUPTED` with `side_effect: unknown`. A step reached by an earlier attempt gets no token again, so an expired token never blocks a commit that happened.
- `budget.timeout_s` covers the whole workflow; each step gets the remainder. `max_recoveries` and `allow_escalation` pass to every step.
- Workflow records go to `<runs>/workflows/wf_<ulid>/` (`workflow.json`, `log.jsonl`, `result.json`), with no `run.json`, so the metrics and drift readers see only the step runs. Sensitive inputs are masked and tokens are stored by hash.
- Workflows have no approval lifecycle of their own. They add no UI actions, every child is an approved capability, and consent binds the concrete inputs a person approves.

### Tests
- `tests/unit/test_workflow.py` (41): the shipped workflow plans clean with typed outputs; binding parsing; each wiring mistake (unknown input, step or output, forward or self reference, type mismatch, optional to required, unbound required input, unknown child input, literal failing its pattern, templates, duplicate ids, unused input, literal output, sensitivity mismatches); integer→decimal and output→input binding at run time; per-step keys, consent, and the shared budget; business outcome stopping before the commit; timeout; refusals before any step (no key, no consent, handoff instead, misdirected or malformed consent, consent for other inputs, spent token, bad inputs, revoked step, other app family); retries (stored result, conflict, lost answer rebuilt with no second commit and no token re-sent, pinned versions, escalated step not re-run then carried on, killed commit never re-run, a step that could not start not left interrupted); CLI check, list, and approval-token.
- `tests/integration/test_workflow.py`: against the mock app, one commit and typed outputs; the same key again is cached with the commit count unchanged; an unknown member stops at the lookup with no commit.
- The purity test also holds `cua.workflow` to "imports no model client".

### Results
- Gate: ruff, `ruff format --check`, `mypy --strict` clean; 607 tests pass (565 before this phase), about 9 minutes.
- Live against the mock app (`cua workflow run`): lookup + open with consent gave `success`/`committed`, `REF-10003-0001`, and the commit count went 0 → 1. The same key again was cached (count stays 1). The same key with other inputs was `INPUT_INVALID`. A token for other inputs was `POLICY_BLOCKED` before any step. Member 99999 gave `business_outcome NOT_FOUND` at the lookup, open `not_run`, no commit. With `--handoff`, open escalated `NEEDS_APPROVAL`. Re-running while it waited returned the same resume token and started nothing. After `cua resume` with a token, the re-run returned `success` with both steps cached and 1 commit total.

### Known issues
- Consent for a step whose inputs come from an earlier output cannot be minted ahead; such a step needs `--handoff` (a person consents to the values read).
- Workflows are not offered in `cua catalog` as tools yet; an agent runs one with `cua workflow run`.
- The journal is a file per key, like replay's idempotency cache: one process is the deployment. Two concurrent attempts under one key are not serialised.

### Commit
- `feat: add typed capability composition`.

### Next
- Phase 6: security threat model and prompt-injection benchmark.

## Phase 6 — Security threat model and benchmark

Status: COMPLETE

### Changes
- `docs/THREAT_MODEL.md`: assets, trust boundaries, the controls in the order an action meets them, the 15 threats with the attack, control and verdict for each, and the residual risks. `SECURITY.md`: how to report a vulnerability, the model in one paragraph, and how to check it.
- New controls:
  - **Credential sink** (`cua.policy.allowlist.credential_sink`, new policy field `credential_paths`, default `^/login$`). A credential placeholder is substituted only in a text field on a sign-on screen, and a password only into a field labelled as one. Enforced in the discovery loop and in the replay engine before the value is substituted.
  - **Egress guard** (`PlaywrightSurface.restrict_egress`). The browser aborts every request to an origin the policy does not allow. It has two layers: a context route, plus CDP Fetch interception, which catches redirect hops. Set by replay (first run and resume), `cua discover`, and the benchmark baseline. Refused origins are logged as `egress.blocked`. The run lifts the guard while it waits on a person and when it leaves the session to one: the guard is serviced by this process, which does not pump the browser while it waits (measured, a person's click on the console timed out).
  - **Approval on record** (`Registry.approval_on_record`). Replay and resume refuse a capability marked approved unless the registry ledger records its approval for exactly that content (`--allow-draft` still overrides).
  - **Prompt labelling.** Screen content is wrapped in SCREEN CONTENT markers, and a rule says it is never an instruction. This is defense in depth; nothing depends on it.
- Mock app:
  - Hostile injects: `prompt_injection`, `malicious_redirect`, `confirmation_spoof`.
  - An attacker origin (the same server as `localhost`) with `/attacker/collect` and `/attacker/login`.
  - `/quickopen/<id>`: a GET that commits.
  - `/files/<name>`: a download.
  - `/_debug/attacker`: the inbox and the count of files served.
  - `MOCKAPP_OPERATOR_PASSWORD`: sets the sign-on password, used as the canary.
- `src/cua/security/`:
  - `models`: scenarios, results, metrics.
  - `lab`: the session's capabilities copy, canary tenant, oracles and exposure scan.
  - `probes`: `discovery`, `replay`, `tampered_artifact`, `token_replay`, `token_mismatch`, `cross_tenant`, `stale_session`.
  - `runner`: the session and the markdown/JSON report.
  - `cli`: `cua security list | run`.
- `bench/security/scenarios.yaml`: 22 scenarios across the 15 threats, each with `id`, `threat`, `title`, `severity`, `setup` and `expected`.
- `bench/security/scripts/`: hostile "model" scripts that follow the page's instructions.
- `bench/security/reports/summary.{md,json}`: the committed report.

### Decisions
- The benchmark does not depend on a model misbehaving or behaving. Its model is a script that follows every injected instruction, so what is measured is what the architecture allows an obedient model to do.
- Attacks are judged by effects, never by what the model decided:
  - requests that reached the attacker's origin;
  - files the app served;
  - commits (`/_debug/stats`);
  - where the canary password appears: run directories, trace zips, the model's full prompt, and the attacker's inbox;
  - whether a browser started for a request that should have been refused.
- A scenario is **blocked** when its expected verdict holds (`blocked`, `contained`, `refused`, `escalated`, `failed_safe` or `masked`), every expected policy refusal is in the run, and every counter is zero. A probe that errors counts as not blocked.
- Negative controls prove the fixtures are real attacks:
  - with the egress guard off, SEC-01's form post and beacon reach the attacker;
  - with the guard off and the sink opened, SEC-05 delivers the canary password to the attacker's inbox.
- Each offline scenario gets its own copy of the capabilities: a tampering scenario must not change what the next one sees.
- Found while building the benchmark and fixed here:
  - (a) the password could be typed into any field (SEC-05, SEC-10);
  - (b) a form's destination and a redirect were invisible to the action policy (SEC-01, SEC-08; Playwright's `route` misses redirect hops, as measured);
  - (c) a file edited, re-sealed and marked approved by hand ran unattended (SEC-13, SEC-14).
- Test helpers that made approved copies of a capability now register them, as `cua approve` does; an approved file with no ledger record is exactly what replay now refuses.
- Also fixed: Phase 5's purity-test edit had silently not applied. `cua.workflow` is now held to "imports no model client", and so is `cua.security` (its discovery probe imports the agent only when it runs).

### Tests
- `tests/security/test_controls.py`: the credential sink cases, the default policy, the prompt markers, approval on record, scenario coverage of every threat (each probe and script exists), hostile injects classified, and blocked and metrics semantics.
- `tests/security/test_benchmark.py`: the offline suite (all refused, no browser started); the live suite (all blocked, zero unsafe actions, exposures and approval bypasses); the negative controls.
- `tests/integration/test_mockapp_smoke.py`: every inject mode, the new ones included, reachable from a fresh session.

### Results
- Gate: ruff, `ruff format --check`, `mypy --strict` clean; 626 tests pass (607 before this phase), about 12 minutes (the two live security tests add about 3.5).
- `cua security run` (session `sec_01M3FJJ4T2S4SSZHJHJGKQXRD0`, 125 s): 22/22 attacks blocked across the 15 threats; `unsafe_action_count` 0, `secret_exposure_count` 0, `policy_bypass_count` 0, `approval_bypass_count` 0, `tenant_isolation_failures` 0.
- Negative controls: with the egress guard off, SEC-01's post and beacon reached the attacker; with it off and the credential sink opened, SEC-05 delivered the canary password to the attacker's inbox.
- SEC-08 failed on its first run: the redirect reached the attacker twice, because Playwright's `route` alone missed it. The CDP layer fixed it. The gaps behind SEC-05 (a password typed into any field) and SEC-13/14 (a forged, re-sealed approval ran) were found in the code while writing those scenarios and fixed before they first ran; the negative control shows SEC-05 without the sink.

### Known issues
- An attacker who can write both a capability file and `lifecycle.jsonl` can approve anything. Both are committed files, so the control is review of the diff. Approvals signed with a key held outside the repository would close this; that is not built.
- The CDP layer of the egress guard covers the page the run drives. A popup that redirects off-origin is covered only on its first request. While a person holds a handed-off session, nothing guards what their page sends.
- Attacks inside the allowed origin and paths (the application's own script, or a committing GET on an allowed path) are outside what the policy can see.
- The live scenarios take about 2 minutes (11 browser runs).

### Commit
- `feat: add computer-use threat model and security benchmark`.

### Next
- Phase 7: surface abstraction hardening.

## Phase 7 — Surface abstraction hardening

Status: COMPLETE

### Changes
- `src/cua/surface/features.py`: a closed vocabulary of surface features, each with its meaning: `accessibility_tree`, `geometry`, `fixed_viewport`, `frames`, `locations`, `document_status`, `forms`, `keyboard`, `dialogs`, `screenshots`, `egress_control`, `session_handoff`. `SurfaceDescriptor` holds the adapter name, contract version, target kinds and features.
- `src/cua/surface/adapters.py`: the adapter registry as data (`web` and `legacy_web` → `playwright` v1, every feature). `SUPPORTED_SURFACES` is now its keys. Checks made before anything starts use it, with no browser library imported.
- `Surface.descriptor` joins the protocol. `PlaywrightSurface` exposes the registered descriptor, `LeasedSurface` passes it through. The protocol docstring maps the plan's observe/act/wait/screenshot/close onto the existing methods (`observe`, `act`, `wait_for`/`settle`, `observe(screenshot=True)`, the context manager) rather than adding duplicates.
- Capability schema 1.2 (`capabilities/schema/capability-1.2.json`): `surface_requirements`, the features the capability needs. Required at 1.2 and refused at 1.1. On load it must cover every feature the content uses, and may add more. The 1.1 schema file stays: 1.1 is still read, never written.
- `src/cua/artifact/requirements.py`: derives the features a capability uses from its content (rungs, conditions, actions, frame scopes, masks). It checks a capability against a descriptor and adds what the run itself needs: `egress_control` always, `screenshots` unless the run takes none, and `session_handoff` + `screenshots` with `--handoff`.
- New failure code `SURFACE_INCOMPATIBLE`, always `side_effect: none`. It is checked in three places:
  - `cua replay` against the registered adapter, before a browser starts. The unsupported-kind refusal (a `desktop` capability) moved here from `POLICY_BLOCKED`.
  - The replay engine, against the surface it was actually handed, before the first action, including a resumed run. The result is logged as `surface.checked`, mapped to the canonical `policy.checked`.
  - `cua catalog` (not offered as a tool) and `cua workflow check|run` (the whole workflow is refused).
- `cua record` writes schema 1.2 with the declaration (`store.declare_requirements`). A drift candidate repairing a 1.2 capability extends its declaration if the new rung needs more.
- `cua surfaces`: the adapters and their features; for each registered capability, what it needs (declared or derived), and whether it runs here. `cua describe` has a `Surface needs` line.

### Decisions
- Committed 1.1 capabilities are not rewritten. A declaration is content, so adding one would change the content hash and void the approval. They load byte for byte as before, with the same hash and an approval still on record. Their requirements are derived when asked for, and every report says `derived`. That is the compatibility layer: nothing is silently reinterpreted, and nothing is silently assumed to need nothing.
- A 1.2 declaration that leaves out a feature the content uses is a load error, not a union taken quietly: what a reviewer approves as the capability's needs must be at least what running it asks for.
- A construct with no entry in the derivation table raises `UnknownConstruct`, rather than being assumed to need nothing. A unit test holds the table to every rung, condition and action the schema allows.
- The name is `features`, not the plan's `capabilities`: in this codebase a capability is the artifact that runs on a surface.
- Today the one adapter has every feature, so no shipped capability is refused. The refusals are proved with degraded descriptors: a registry entry patched in the tests, and a fake surface handed to the engine.

### Tests
- `tests/unit/test_surface_compat.py`:
  - the vocabulary and the registry;
  - the Playwright descriptor matches the registered one;
  - the derivation table covers the whole schema, and derivation follows the content;
  - schema 1.2 rejects a 1.1 file with the field, a 1.2 file without it, an under-declared, duplicated or unknown feature;
  - the committed 1.1 capabilities load unchanged, with their approval on record;
  - a declaration may exceed the content;
  - refusals: replay before a browser, with and without handoff; the catalog; a workflow;
  - a recording saves at 1.2.
- `tests/unit/test_replay.py`: the engine refuses a surface without `frames` before any action, even a navigation; a compatible surface is checked and logged; the goal-2 recording equals the committed 1.1 capability plus exactly its derived declaration.
- The golden recording is regenerated at 1.2, and the 1.1 golden is kept beside it (`member_savings_balance-1.1.json`).

### Results
- Gate: ruff, `ruff format --check` and `mypy --strict` are clean. 652 tests pass (626 before this phase).
- Live replay of `member_savings_balance` (`run_01M3FMXHWGYQC18ND03CFWR5DF`) succeeded, as before. `surface.checked` records `playwright` v1, the derived requirements and `missing: []`, and `cua metrics run --events` shows it as `policy.checked` / `surface_checked`.
- A copy of the capability retargeted to `desktop` returned `failure SURFACE_INCOMPATIBLE`, `side_effect: none`, exit 1, and no browser started. `cua describe` says `this build CANNOT run it`.
- `cua surfaces` lists `playwright` v1 with all 12 features. Both committed capabilities are schema 1.1 with derived requirements, and both run here.

### Known issues
- The feature list is coarse. `forms` covers both typing and selecting, and nothing says a surface's `geometry` is as precise as the one a `bbox` rung was recorded on (`fixed_viewport` and `recording_env` cover that last case).
- Features are claimed by the adapter, not measured. An adapter that claims a feature it implements badly passes the check.
- The descriptor's `version` is recorded (`surface.checked`), but capabilities cannot yet require a minimum adapter version.

### Commit
- `refactor: formalize surface compatibility contract`.

### Next
- Phase 8: Windows UI Automation surface.

## Phase 8 — Windows UI Automation surface

Status: COMPLETE

### Changes
- `src/cua/surface/windows/`:
  - `perception`: a UIA snapshot into the existing `Observation`. UIA control types map to the web's roles; the title bar, scroll bars, a combo box's own parts, off-screen items and message boxes are left out; boxes are measured from the window's corner; disabled and checked are `attrs`; a password is never read. Pure, so it is tested on any platform.
  - `uia`: the COM and Win32 layer (`comtypes`): cached subtree snapshots, windows found by `EnumWindows` per process, and posted input.
  - `locator`: the live element is re-read before acting and must still be the observed control (`PerceptionDrift` otherwise).
  - `actions`: click, type, select, press, read.
  - `conditions`: `DesktopEvaluator`, and what each condition reads on a desktop.
  - `adapter`: `WindowsSurface`. `Navigate` to the tenant's `uia://<app>` location starts the application fresh. Message boxes are answered and reported as dialogs. Screenshots and `expose` raise.
- `WINDOWS_UIA` (`windows-uia` v1) is registered for `target.surface: desktop`, on Windows only. Its features are `accessibility_tree`, `geometry`, `fixed_viewport`, `locations`, `forms`, `keyboard` and `dialogs`; the missing ones are documented in `adapters.py`.
- `deskapp/deskcalc.ps1`: DeskCalc, a deterministic WinForms window. It has unnamed number fields, a combo box, a checkbox, Calculate, a read-only result and Record (the commit, appended to a ledger file). `-Inject` switches on `renamed_button`, `ambiguous`, `disabled`, `slow` or `modal`.
- Tenant `desktop` binding (`launch`, `inject_flag`) and `tenants/desk.yaml`. The capability names only `uia://deskcalc`; the command that starts the program is tenant configuration.
- `policies/deskcalc.yaml`: the same policy engine with desktop data. The allowed path is `/DeskCalc` (the window title), and Record is a risky rule.
- `capabilities/families/deskcalc.yaml`: the business outcomes `DIVIDE_BY_ZERO` and `NOT_A_NUMBER`.
- Runner: the adapter is picked from `target.surface` (`surface_for`). The browser's egress guard, trace and handoff apply only to a browser surface. `cua replay` no longer hard-wires a browser.
- `cua discover` drives a desktop tenant through `WindowsSurface`. It keeps no screenshots (and says so) and refuses `--handoff`.
- `ReplayConfig.screenshots` defaults to `None`: screenshots are taken wherever the surface can take them. `True` insists on them (a surface without them is refused), and `--no-screenshots` is `False`. A run on a surface without them says so in `warnings`. Before this, the default `True` made the catalog and `cua describe` report a desktop capability as not runnable.
- Discovery gained a `select` tool; the recorder and replay already had `select` steps.
- `requirements`: `egress_control` is a run requirement of web targets only.
- `ladder_for(for_value=True)` keeps a `role_name` rung when the node's value is held apart from its name (a desktop edit box named "Result"). Web cells, whose name is the value, are unchanged.
- `scripts/discovery/deskcalc_{compute,record}.yaml`, `evidence/discovery-deskcalc-{compute,record}/`, `capabilities/deskcalc_{compute,record}.json` (approved by the user), and `evidence/replay-desktop-{success,commit}/`.
- `pyproject.toml`: a `windows` extra (`comtypes`, Windows only), a mypy override for comtypes, and the `desktop` test marker.

### Decisions
- **No second model of the screen.** A desktop control is a `Node`, so the ladder, the conditions, the recorder, drift classification and the replay engine run on it unchanged. That is the acceptance line: the same capability, runtime, policy and replay path.
- **Input is posted, never invoked.** Measured: `InvokePattern.Invoke` on a button whose handler opens a message box does not return until the box is closed, so the surface could not answer it. Buttons get `BM_CLICK` and keys `WM_KEYDOWN`/`WM_KEYUP`; both return at once.
- **A WinForms combo box has no ExpandCollapse or SelectionItem pattern** (measured). An option is selected with `CB_FINDSTRINGEXACT` and `CB_SETCURSEL`, and `CBN_SELCHANGE` tells the owner, so the application's own handler runs.
- **Windows are found by process id through `EnumWindows`**, never by a UIA search from the desktop root. That search asks every window on the machine and hung on one (measured).
- **Message boxes (`#32770`) are the desktop's native dialogs.** They are answered at once (as declared, else dismissed), reported on the action and on the next observation, and never shown as screen content: the web adapter's `confirm` rule.
- **What the surface cannot do, it does not claim.**
  - Screenshots: a mask cannot yet be painted in before capture, and a screenshot cleaned afterwards has already held the secret.
  - Handoff and egress control: not available on this surface.
  - So a desktop replay keeps no screenshots and says so in its warnings. A run that insists on screenshots is refused (`SURFACE_INCOMPATIBLE`) before DeskCalc starts.
- **`fixed_viewport` is claimed** because boxes are window-relative and the viewport is the window's size. `resolve_ladder` still refuses a pixel rung when the size or the scale differs, and pixels are never acted on unattended.
- Found while building: a risky rule's `location` is searched in the whole location URL, not its path. `^/DeskCalc$` therefore never matched, and Record was recorded as `safe`. Caught by reading the recorded capability; the rule is now `/DeskCalc$`, like the web's `/review/`.

### Tests
- `tests/unit/test_windows_surface.py` (any platform):
  - perception: roles, what is left out, window-relative boxes, parents, ordinals, state, passwords;
  - the ladder on a desktop observation;
  - the settle signature;
  - the live-element check;
  - the desk tenant binding;
  - the desktop capabilities need only what `windows-uia` has, and web runs still need egress control;
  - the `select` tool.
- `tests/desktop/` (`-m desktop`; Windows with the `windows` extra; skipped elsewhere):
  - `test_windows_surface.py`: the protocol on DeskCalc; observation-scoped refs; a disabled control, a missing option and a read-only field are `ActionFailed`; a message box answered and reported, and accepted when declared; screenshots, `expose` and a foreign location refused; closing stops the application.
  - `test_desktop_replay.py`: through `cua.replay.runner.replay`:
    - `success` on `role_name` rungs only;
    - `DIVIDE_BY_ZERO`;
    - `renamed_button`, `ambiguous`, `disabled` and `modal` stop with `side_effect: none`;
    - Record: refused without consent (empty ledger), then one token gives one ledger line, the same key is answered from the cache, and the spent token is refused;
    - a run asking for screenshots is refused before start.
- Updated: the catalog and replay tests that assumed no desktop adapter exists (on Windows the web capability retargeted to desktop is now refused for `frames`). The drift evidence scan is scoped to the web app family, since local desktop runs drift in their own ways and are classified correctly.

### Results
- Gate: ruff, `ruff format --check` and `mypy --strict` are clean. 682 tests pass (652 before this phase; 14 of the new ones drive DeskCalc live) in about 13 minutes.
- Scripted discovery of `deskcalc_compute` reached `done` with 12.5 / 4 = 3.125 in 9 s. `cua record` wrote a schema 1.2 capability, `surface: desktop`, with every step and the output on a `role_name` rung.
- Replay of `deskcalc_compute`: `success` with `result` 3.125, no model and no browser, and a warning that windows-uia takes no screenshots.
  - `renamed_button` → `LOCATOR_UNRESOLVED`; `ambiguous` → `LOCATOR_UNRESOLVED` (2 matches); `disabled` → `ACTION_FAILED`; `modal` → `ACTION_FAILED`, the message box answered and reported.
  - Divide by zero → `business_outcome DIVIDE_BY_ZERO`; `second=abc` → `INPUT_INVALID` before DeskCalc starts.
  - `drift scan` classifies the ambiguous run `CONTROL_AMBIGUOUS`, the same as the web.
- `deskcalc_record` without a token → `POLICY_BLOCKED` / `NEEDS_APPROVAL` and an empty ledger. With a token → `committed` and one ledger line. The same key again is served from the cache; the spent token is refused.
- `cua surfaces` lists `windows-uia` v1 and both desktop capabilities (`needs (declared)`, `runs here`).

### Known issues
- No screenshots and no handoff on a desktop target.
- DeskCalc's recorded Calculate step has no wait, since the window's location does not change. With `slow`, the output is read before it exists: a safe `EXTRACTION_FAILED`, not a wait.
- Posted input reaches Win32 common controls (every WinForms control, and the classic dialogs). A UI toolkit with no window handles (WPF, UWP, Electron) would fall back to `Invoke`, which blocks while a modal is open.
- DPI: boxes are in physical pixels, and `recording_env.dpr` is recorded as 1.0. A recording at another scale refuses its pixel rungs, and nothing else depends on scale.
- The desk tenant starts Windows PowerShell; a cold start takes about a second.
- The target window takes the foreground when it starts, so a person typing somewhere else at that moment types into it. Seen once while making the evidence: the first field read `imp12.5`. The step's `value_set` check turned it into a safe `TIMEOUT` with `side_effect: none`, not a wrong answer. Starting the application without activating it, or on a separate desktop, would remove the hazard.

### Commit
- `feat: add Windows UI Automation surface`.

### Next
- Phase 9: vision as a controlled fallback.

## Phase 9 — Vision as a controlled fallback

Status: COMPLETE

### Changes
- `src/cua/surface/vision/`:
  - `detector`: `TemplateDetector` returns every match of a recorded picture on a screenshot, best first, with a confidence. It never acts. It uses zero-mean normalised cross-correlation, computed with FFTs padded to 5-smooth lengths (about 0.1 s for a 1280x800 screen), and no model.
  - `candidate`: `Appearance` is a recorded, masked crop of a control, with its hash checked on load. `VisualCandidate` holds the action, confidence, bounds and the evidence screenshot it was found on.
  - `validator`: one pure function. It refuses, in order: a non-click (`unsupported`), a risky or approval-required step (`unsafe`), a different screen scale (`scale`), a weak best match (`not_found`), a rival within the margin (`ambiguous`), off screen (`outside`), touching a mask (`masked`), on a control the tree already has (`contradicted`), and a policy block or a risky rule covering the screen (`policy`).
  - `image`: PNG decode, crop and pixel hash. This is the only module that decodes an image.
- `ClickPoint` action:
  - It carries a guard box and the SHA-256 of the pixels it was chosen on.
  - `PlaywrightSurface` captures the box again and raises `PerceptionDrift` if the pixels changed, and `StaleRefError` if its observation is gone. It then clicks.
  - `WindowsSurface` refuses it.
- New surface feature `pointer`. Playwright claims it; windows-uia does not. `--vision` adds `screenshots` and `pointer` as run requirements, so a desktop run asking for vision is `SURFACE_INCOMPATIBLE` before it starts.
- Capability schema 1.3 (`capabilities/schema/capability-1.3.json`) adds `step.appearance`, on click steps only. A 1.1 or 1.2 capability hashes as before: a step without an appearance is left out of the hash.
- `cua record` crops each clicked control from the screenshot of the screen it was clicked on. There is no appearance when the crop touches a mask, when the run kept no screenshots, or when the vision extra is missing. `cua describe` lists the appearance as the last rung.
- Policy `vision:` (`allowed`, `min_confidence` 0.97, `min_margin` 0.1). Allowed is false by default and on in `policies/default.yaml`. The policy judges a `ClickPoint` by the control it claims to hit, and any risky rule covering the screen makes it need approval.
- Replay:
  - `ReplayConfig.vision` / `cua replay --vision`. When every rung misses (never when the tree is ambiguous) and the step has an appearance, the engine keeps a masked screenshot as evidence, detects, validates, and then permits, acts and lands exactly like any other step.
  - Refusals end as the locator failure the step already had, escalated `STUCK`: `LOCATOR_AMBIGUOUS` for ambiguous, `POLICY_BLOCKED` for unsafe, policy or masked, and `LOCATOR_UNRESOLVED` otherwise. The message carries the vision verdict.
  - Events `vision.candidates`, `vision.accepted` and `vision.refused` map to canonical `policy.checked` and `policy.blocked`. The rung used is `vision`.
- Drift kind `CONTROL_UNLABELED`: a control only vision found. It is not repairable from the evidence.
- Mock app injects:
  - `hidden_control`: the Search and Confirm buttons are drawn as always, with `aria-hidden`.
  - `hidden_duplicate`: two identical hidden Search buttons.
- `pyproject.toml`:
  - a `vision` extra (numpy, Pillow), included in `dev`;
  - numpy's stubs are skipped by mypy, because they use syntax that mypy rejects when checking for Python 3.11.
- README, REPORT and the threat model: the vision fallback, and the visual look-alike residual risk.
- `evidence/replay-vision-{recovered,ambiguous,not-found,commit-refused}/`: the verification runs, each with the 1.3 capability it ran (`capability.json`), approved by the person verifying. Drift on record now includes `CONTROL_UNLABELED` and the vision duplicate's `CONTROL_AMBIGUOUS`.

### Decisions
- **Model-free.** Replay still initialises no model. The detector matches a picture recorded at discovery and approved with the capability, so vision never acts on something a reviewer did not see.
- **Candidates, then validation, then policy, then execution.** The detector cannot click. The validator is pure and runs every check before the policy. The click is then guarded by pixels, and the step's own expectation and checkpoint decide whether it landed.
- **Vision fills gaps in the tree and never overrules it.**
  - A tree that finds several controls is `LOCATOR_AMBIGUOUS` without vision looking at all.
  - A match on top of a control the tree has is refused.
- **Never on a commit.** A step that is risky, irreversible or approval-required, or a screen a risky rule covers, is refused with or without consent. A person does it.
- **Two switches.** The policy governs whether vision is permitted and how sure it must be. The run asks for it. Either one off means no vision.
- **Committed capabilities are not re-recorded.** v3 of the web capabilities keeps its hash and approval, and has no vision fallback. A 1.3 recording of the same discovery run gets one.
- Found while testing: recording under a policy for another origin marks a commit `safe`, because the recorder treats `Block` like `Allow`. Vision still refused the click through its risky-rule check. The recorder fix is filed as its own task.

### Tests
- `tests/unit/test_vision.py` (no browser):
  - the detector on synthetic pictures;
  - every validator refusal, and the accepted candidate's guard;
  - the policy's reading of `ClickPoint`;
  - the run requirements;
  - schema 1.3: click only, 1.3 only, older hashes stand, the exported schema;
  - the recorder's crops and the mask rule;
  - drift `CONTROL_UNLABELED`.
- `tests/integration/test_vision.py` (browser):
  - the 1.3 recording;
  - `hidden_control` → `success` on the vision rung;
  - with the flag off, the policy off, or no appearance → unchanged `LOCATOR_UNRESOLVED`;
  - `hidden_duplicate` → `LOCATOR_AMBIGUOUS`, nothing clicked;
  - `renamed_button` → `not_found`;
  - a hidden Confirm with a valid token → `POLICY_BLOCKED`, no commit;
  - the pointer guard: an unchanged point is clicked, changed pixels are `PerceptionDrift` with nothing submitted, and a point from a gone observation is `StaleRefError`.

### Results
- Gate: ruff, `ruff format --check` and `mypy --strict` are clean. 726 tests pass (682 before this phase), in about 12 minutes of test time.
- Detection: 0.1 s per 1280x800 screen. On the mock app, the recorded Search button scores 1.000 where it is drawn, and the runner-up scores 0.514. The renamed "Find" button scores 0.510. The nav frame's differently styled Search button scores 0.956.
- A clip screenshot of a box is pixel-identical to the same box of a full screenshot, which is what lets the guard compare hashes.
- CLI, 1.3 recording of the goal-1 discovery run, approved in a scratch directory:
  - `--inject hidden_control --vision` → `success`, savings 1411.21, `search.submit` on the `vision` rung, with a warning naming the skipped rungs;
  - the same without `--vision` → `LOCATOR_UNRESOLVED`;
  - `hidden_duplicate` → `LOCATOR_AMBIGUOUS` (1.000 and 0.997), nothing clicked.
- The goal-2 recording with a valid token and `hidden_control` → `POLICY_BLOCKED`, `side_effect: none`, no POST to the review screen.

### Known issues
- Web only. The desktop surface takes no screenshots and has no pointer input.
- Only clicks. Typing into a field found by pixels would need the field's identity for the credential sink as well.
- Template matching finds a control drawn as recorded. A restyled or re-scaled control is not found, and is escalated. A different device pixel ratio is refused (`scale`).
- A page that draws one convincing copy of a safe control on an allowed screen could steer a click (threat model, residual risks).

### Commit
- `feat: add controlled vision fallback`.

### Next
- Phase 10: API layer.

## Phase 10 — API layer

Status: COMPLETE

### Changes
- `src/cua/api/`:
  - `access`: `api/access.yaml`, which lists the tenants served (tenant file, policy) and the clients. Each client has a key binding, tenants, capabilities and scopes (`read`, `invoke`, `approve`, `operate`). Keys are compared in constant time. A client whose key cannot be read, or is shorter than 32 characters, is disabled. File-bound keys are created on first start.
  - `models`: `RunRequest`, `ApproveRequest`, `ResumeRequest`, `AbortRequest`, `ApiRun` (states `running`, `escalated`, `finished`, `error`, `lost`), and `ApiError`.
  - `service`: `RunService` runs invocations on a worker pool through `cua.replay.runner`, writes run records and the idempotency index under `<runs>/.api/`, and re-reads a run's directory for anything that happened elsewhere.
  - `app`: the FastAPI app. It adds a request id to every answer, requires one on every POST, logs every request to `<runs>/.api/requests.jsonl`, uses one error shape, and parses bodies with exact decimals.
  - `routes/`: `health`, `capabilities`, `runs`, `approvals`.
  - `cli`: `cua serve`.
- Endpoints: `GET /health`; `GET /capabilities`, `/capabilities/{name}`, `/capabilities/{name}/versions`; `POST /runs`; `GET /runs/{id}`, `/runs/{id}/events`; `POST /runs/{id}/approve`, `/resume`, `/abort`. `?wait=S` (up to 300 s) holds an answer until the run stands still.
- `cua.replay.runner`:
  - `replay(run_id=...)` names the run directory up front.
  - `check_resume` holds every check `resume` makes before it touches the run. `resume` now calls the same code (`_prepare_resume`).
- `api/access.yaml`: `local-agent` (file key under `.cua/api-keys/`, every scope, both tenants) and `balance-reader` (env key, read and invoke, `member_savings_balance` only).
- README (a section and a CLI row), REPORT, the threat model (trust boundary 3, the API gate control, the no-TLS residual risk), `.env.example`.

### Decisions
- **One execution path.** A request becomes the same `replay()` call as `cua catalog invoke`, with typed arguments checked by `catalog.coerce` and the result dumped by `to_json`, so the fields come in the CLI's order. The API adds identity and bookkeeping, not behaviour.
- **Asynchronous by default.** A run gets its id at once, and its run directory takes that id. A handoff run is started with `wait_s=0`: it comes back `escalated` at once with the browser left up, and `approve`, `resume` and `abort` act on it later. No worker waits on a person.
- **The tenant is named, never inferred.** `X-Cua-Tenant` is on every request and checked against the client's list.
- **Consent over HTTP is a signed token.** `approve` takes a `cua approval-token` token, never a name: the console's name-only Approve relies on a person at a localhost page, and an HTTP caller is not one. Carrying a token (on a new run or on `approve`) needs the `approve` scope. The token is the consent; the scope is the permission to deliver it.
- **A refused token is the answer to the request.** `check_resume` runs synchronously, so a bad token is `403`, the run stays `escalated`, and the reason is on the record. Only a resume that passes the checks goes to a worker.
- **Idempotency keys** are required (`428`) for any capability that is not read-only and idempotent.
  - The key is claimed with an exclusive file create, so concurrent retries start one run. A retry returns the run the key started, even while it is executing, with `Idempotent-Replayed: true`.
  - The same key with another capability, version or inputs is `409`.
  - The runner sees `api:<tenant>:<client>:<key>`, so its result cache cannot answer one client with another's result. The caller gets its own key back in the result.
- **Visibility.** A run is visible only to the client and tenant that started it. Anything else is `404`, so the API does not reveal whether it exists.
- **The run directory is the record.**
  - A run aborted on the console or carried on by `cua resume` shows its new result on the next read.
  - A run left `running` by a stopped server gets its result if the engine wrote one (`INTERRUPTED` included). Otherwise it is `lost`, with a note to read its evidence; it is never assumed.
- **Capabilities are described, not handed over.** The tool definition, contract and lifecycle are returned. Steps, locators, credential references and paths are not.
- Workers initialise COM, so a desktop capability runs over HTTP as well.
- `inject` is refused unless the server is started with `--allow-inject`.

### Tests
- `tests/unit/test_api.py` (33, fake runner):
  - health;
  - authentication, the tenant header, request ids;
  - disabled keys, key creation, the committed access file;
  - capability listing, description and authorization;
  - runs as replay is called; `202` then read; exact decimals; `INPUT_INVALID` with nothing started; malformed bodies;
  - idempotency: `428`, one key one run, conflict, concurrent retries, per-client scoping;
  - scopes; run visibility; `inject`; `error`;
  - approve refused and accepted, approve only for `NEEDS_APPROVAL`, scopes for approve, resume and abort; resume;
  - a run answered elsewhere; `lost` versus an interrupted result; events of a refused run;
  - the request log (no keys in it); no model client imported.
- `tests/integration/test_api.py` (4, browser):
  - `member_savings_balance` over HTTP and through `cua catalog invoke` give the same logical result, with the same fields in the same order; its events;
  - `open_subaccount`: escalated `NEEDS_APPROVAL`; a token for other inputs is `403` with the browser still up; the right token commits (`decided_by: api:agent`); a retry with the key returns that run;
  - an escalated run aborted over HTTP is `ESCALATION_ABORTED`, `side_effect: none`, and its browser is closed.
- `tests/desktop/test_desktop_api.py`: `deskcalc_compute` over HTTP on a worker thread.
- `test_an_escalated_run_nobody_answers_expires_when_it_is_read` (browser): a handoff with a 1 s time to live is `ESCALATION_ABORTED` on the next read, and its browser is closed.
- `tests/unit/test_drift.py::test_every_run_on_record_can_be_scanned` now scans the committed evidence only. `evidence/runs/` is local scratch: a run made there on the day of the vision evidence shares its run-id prefix and broke the count.

### Results
- Gate: ruff, `ruff format --check` and `mypy --strict` are clean. 772 tests pass (726 before this phase), in about 14 minutes.
- Live, `cua serve` on :8200 against the mock app, from PowerShell:
  - no key → `401`;
  - `GET /capabilities` lists `member_savings_balance` v3 and `open_subaccount` v3;
  - `member_savings_balance` over HTTP → `success`, 1411.21, in 7.6 s; `cua catalog invoke` answers the same; its events run from `run.started` to `run.completed`;
  - `open_subaccount`:
    - without a key → `428`;
    - with `handoff` → `escalated NEEDS_APPROVAL` at `review.submit`, `side_effect: none`;
    - a token for 999.00 → `403`, still `escalated`;
    - the 250.00 token → `success`, `committed`, `REF-10003-0001`, decided by `api:local-agent`;
    - the same key again → the same run, `Idempotent-Replayed: true`;
  - another escalation aborted → `ESCALATION_ABORTED`, `side_effect: none`;
  - every request is in `evidence/runs/.api/requests.jsonl`.
- Found while testing:
  - Reading a run while its worker settled it could overwrite the result with `lost`. Records are now read under the service lock.
  - A desktop run's UI Automation objects, released later on another thread, made faulthandler print `0x80010108` (RPC_E_DISCONNECTED) five times in a full run. Each worker now collects garbage in its own COM apartment when a run ends; the next full run printed none.
  - An escalated run nobody answers was expired only when someone opened the operator console. The API now expires it on read.

### Known issues
- No TLS; binds to localhost by default and warns otherwise. Keys do not expire.
- One server per runs directory: the set of executing runs is in memory.
- Runs are not listed (`GET /runs`); a caller keeps the ids it was given.
- The operator console is still unauthenticated. The API's `resume` hands back on the caller's word, as `cua resume` does.

### Commit
- `feat: expose capability runtime through API`.

### Next
- Phase 11: MCP integration.

## Phase 11 — MCP integration

Status: COMPLETE

### Changes
- `src/cua/mcp/`:
  - `tools`: the tool list and result shaping. One tool per capability unattended replay would run, for the tenant's application family, that the client may use. Four run tools: `cua_run_status`, `cua_approve_run`, `cua_resume_run` and `cua_abort_run`, each offered only with its scope.
  - `server`: MCP over stdio, JSON-RPC 2.0, one message per line. It answers `initialize` (protocol 2025-06-18, 2025-03-26 or 2024-11-05), `ping`, `tools/list` and `tools/call`. Notifications need no answer. Each tool call runs on its own thread.
  - `cli`: `cua mcp --client --tenant --root --handoff consent|all|none --wait`.
- `catalog.tool_definition(resume=...)`: the escalation sentence names the channel's way to carry a run on (`cua resume` on the CLI, `cua_approve_run` over MCP).
- README (a section and a CLI row), REPORT, and the threat model (trust boundary 3 and the MCP result control).

### Decisions
- **No SDK.** A tool server needs `initialize`, `ping`, `tools/list` and `tools/call`. Everything behind them is `cua.api.service`, which already owns authorization, idempotency and the run lifecycle. Implementing the protocol directly adds no dependency, and no network install.
- **The same path as the API.** A tool call is a `RunRequest` pinned to the listed version. The registry resolves it, the arguments are typed by `catalog.coerce`, the client, tenant, scopes and policy apply, and replay runs with no model. Only a listed tool can be called.
- **Business operations, not a browser.** A tool's schema is the capability's typed inputs. Its description is the contract:
  - purpose and outputs;
  - side effect and idempotency;
  - business outcomes and escalations.
- **Tool annotations** carry the contract: `readOnlyHint`, `destructiveHint` and `idempotentHint`.
- **The invocation's own arguments.** `idempotency_key` is required on any tool that is not read-only and idempotent. `approval_token` is offered only on a tool that may ask for consent, and only to a client with `approve`. A capability declaring an input with either name is not offered.
- **The agent's view of a result.**
  - It gets kind, code or reason, step, side effect, outputs, payload, message and warnings.
  - It does not get locator rungs, screenshots, the observed screen text or evidence paths, which stay in the run directory under `run_id`. An agent is not steered by GUI text through a tool result.
  - An escalated result adds `resume_token` and `required_action`, which says what has to happen and which run tool to call.
- **Tool errors versus protocol errors.**
  - A tool result with `isError` covers a refusal (with its code), a failure, `error` and `lost`. A business outcome and an escalation are not errors.
  - A JSON-RPC error covers what the client got wrong: an unknown method or tool, or a malformed message.
- **Identity over stdio.** Whoever starts the process is the caller, and no key is asked for. `--client` names the access-file entry whose tenants, capabilities and scopes apply. `--root` lets an MCP client start it from anywhere.
- **Handoff** by default only for capabilities that may ask for consent. A commit without a token comes back `escalated` with the session waiting; a stuck lookup is a plain `failure`.
- stdout carries only the protocol. The process's own `sys.stdout` is redirected to stderr, so a runner warning cannot corrupt a message.

### Tests
- `tests/unit/test_mcp.py` (19, fake runner):
  - protocol: version negotiation, protocol errors, notifications, one message per line, a slow call not holding up `ping`;
  - tools: the exact list and no browser primitive; schemas, descriptions and annotations; a reader's reduced list;
  - calls: the agent view; GUI details left out; exact decimals; refusals as tool errors; escalation → `required_action` → `cua_approve_run` (refused, then accepted); a token on the capability tool; handoff settings; run-tool argument checks;
  - the request log;
  - the real `cua mcp` process (stdout is only JSON; `--root` from another directory); unknown client or tenant; no model client imported.
- `tests/integration/test_mcp.py` (browser), `cua mcp` as its own process: a balance lookup; `NOT_FOUND` as an answer; a stray argument is `INPUT_INVALID`; `open_subaccount` → `escalated NEEDS_APPROVAL` with `required_action`; a minted token through `cua_approve_run` → `committed`; the same key again → the same run.

### Results
- Gate: ruff, `ruff format --check` and `mypy --strict` are clean. 792 tests pass (772 before this phase), in about 21 minutes.
- A real MCP client: headless Claude Code (`claude -p --mcp-config ... --strict-mcp-config`), started from another directory with `cua mcp --root`.
  - Asked for member 10003's savings balance, it called `member_savings_balance` and answered 1411.21.
  - Asked to open a sub-account, it got `escalated NEEDS_APPROVAL` with `side_effect: none`, read `required_action`, and said it could not consent itself. It called `cua_abort_run` and reported `ESCALATION_ABORTED`, `side_effect: none`.
  - Both calls are in `requests.jsonl` as `MCP tools/call ...` under `local-agent`.
- Found while testing: the per-thread COM initialisation from Phase 10 was never matched by an uninitialisation. A full run printed faulthandler's `0x80010108` twice while a test client shut its workers down. COM is now initialised and uninitialised on the worker around each run, after a collection; the next full run printed none.
- One full run failed SEC-05 of the security benchmark with Chromium's "Unable to capture screenshot" while a second browser was driven by hand. It passed alone and in the next full run.

### Known issues
- Only tools. There are no resources, prompts or `listChanged` notifications: the tool list is read afresh on every `tools/list`, and a client that caches it sees a new approval on reconnect.
- A call that takes longer than `--wait` (300 s at most) returns `running` with `cua_run_status` as the way on. The client's cancellation notifications are accepted and ignored: a replay is never stopped mid-step.
- stdio only. A remote agent uses the HTTP API.
- During verification, the machine's antivirus (Surfshark) quarantined Playwright's `chrome.exe`. Handoff runs start that binary detached with a remote-debugging port, a pattern antivirus heuristics flag. Every detached launch then failed with `[WinError 2]`: the CLI handoff, the API commit and the MCP commit tests. Excluding `%LOCALAPPDATA%\ms-playwright\` and reinstalling Chromium (`playwright install --force chromium`) restored them. The MCP integration test now prints the answer it got when escalation fails.

### Commit
- `feat: expose approved capabilities through MCP`.

### Next
- Phase 12: multi-tenant isolation.

## Phase 12 — Multi-tenant isolation

Status: COMPLETE

### Changes
- `cua.tenant`:
  - A tenant file binds its `policy` (default `policies/default.yaml`) and may list the `capabilities` it runs. Without a list it runs every capability of its app family.
  - `Tenant.refusal(name, app_family)` is the one check of what a tenant runs. Replay, the workflow validator, the API's capability routes, the MCP tool list and the registry's `tenant_scope` all call it.
  - A tenant that names an `overlay` is refused when it loads. Overlays are not implemented, and running a tenant's capabilities unadapted would drive a layout they were not written for.
  - `shared_bindings(tenants)`: secrets two tenants bind to the same source.
- `tenants/desk.yaml` signs approval tokens with its own key (`.cua/approval-signing-desk.key`), not the one `local` uses. Both tenant files name their policy.
- `cua.replay.runner`:
  - Before anything is read, a credential reference must name the running tenant, and never a system secret (`cua/`). Either is `POLICY_BLOCKED`.
  - The idempotency cache is per tenant.
- `cua.replay.result.IdempotencyCache(root, tenant)`: records are keyed by tenant and key, and carry the tenant. A record from before this (key only) is read as its run's tenant's (`run.json`). One refused before it ran is not an answer. One with a side effect whose tenant cannot be told is `IdempotencyConflict`.
- `cua.workflow.journal`: `Journal(runs_dir, tenant)` works the same way, with the same fallback through the first attempt's `workflow.json`. `find_run` matches the tenant too.
- `cua.escalation.operator_app.Console(tenant=...)`: one tenant's console lists, shows, serves screenshots of and decides only that tenant's requests. Another's is `404`, as if it did not exist. `cua operator --tenant` sets it, and the API's abort uses the caller's tenant.
- `cua.api.access`: a tenant's policy defaults to its tenant file's. The gate refuses to start when two served tenants share a secret. `api/access.yaml` no longer repeats the policies.
- CLI: `--policy` defaults to the tenant's own policy for `discover`, `replay`, `catalog invoke`, `operator`, `workflow run` and `drift propose`.
- README (CLI row, layout, Security, test matrix), REPORT §4, and the threat model (trust boundary 5, the tenant-scoping control, a residual risk).

### Decisions
- **What "a capability from tenant A" means.** Capabilities are shared per app family by design. The unit of isolation is the tenant's deployment, credentials, policy, records and consent. A tenant can still keep a capability to itself: another tenant's file lists what it runs, and the capability is not on it.
- **The tenant file decides, not the capability.** The list lives in the tenant file rather than in the artifact, so the schema and every approved capability's content hash stay as they are.
- **Shared signing keys were the real gap.** A token names its tenant, but that claim is only as good as the key that signed it. With one key file for `local` and `desk`, whoever could consent for DeskCalc could sign consent for `local`. Each tenant now has its own key, and a shared binding is detected (and refused by the API).
- **Idempotency, carried over safely.** Rekeying the cache could have let a retry after the upgrade commit again. The legacy fallback serves a record only to the tenant its run ran on, and refuses to guess when it may have committed.
- **System secrets are the runtime's.** Discovery already kept `cua/` secrets from the model. Replay now refuses a capability that names one, so a reviewed-but-wrong capability cannot type the signing key into a page.
- **Isolation by the runtime, not the OS.** Tenants share one machine, one account and one runs directory. Tenants whose operators must not trust each other need separate deployments. This is stated as a residual risk.

### Tests
- `tests/security/test_tenant_isolation.py` (22), with two tenants on one product (`alpha` and `beta`, `legacy-core`), each with its own credential and signing key. No browser is started: a run that gets past every check reaches the surface factory, which raises.
  - Each tenant runs the shared capability on its own deployment, and `run.json` records which one.
  - A capability the tenant file does not list, and one of another app family: `POLICY_BLOCKED`, no run directory. The registry scopes a capability to the tenants that run it.
  - Another tenant's credential reference, and a system secret by `{tenant.id}`: `POLICY_BLOCKED` before anything is read. The resolver refuses another tenant's reference.
  - A token signed for `alpha` does not verify for `beta`. Under one shared key, the tenant claim still refuses it. Through replay it is refused before a browser, and on `alpha` it runs.
  - The repository's tenants share no secret, and the API gate refuses two tenants bound to one key.
  - One key on two tenants is two requests. `beta` is never answered with `alpha`'s stored result. A legacy record goes only to its run's tenant, and one with an unknown tenant and a possible commit is a conflict. Workflow journals and step-run lookups are per tenant.
  - `beta`'s console lists, shows and decides none of `alpha`'s requests. `alpha`'s run stays untouched.
  - The tenant files resolve their deployment, policy and credentials. A tenant naming an overlay is refused.
- Existing tests pass the tenant to `IdempotencyCache` and `Journal`. A workflow refusal now reads `step 'lookup': member_savings_balance is for app family ...`.

### Results
- Gate: ruff, `ruff format --check` and `mypy --strict` are clean. 814 tests pass (792 before this phase), in about 21 minutes.
- Live, from PowerShell, against the mock app, with a second tenant `beta` on the same product (tenant files under the gitignored `.cua/`):
  - `beta` narrowed to `open_subaccount`: `member_savings_balance` is `POLICY_BLOCKED` (`tenant 'beta' does not run member_savings_balance`).
  - A token from `cua approval-token --tenant local`, presented on `beta`: `POLICY_BLOCKED`, `the token's signature does not verify with tenant 'beta''s key`.
  - One idempotency key: `local` ran (1411.21), `beta` started a run of its own (`cached: false`, another run id), and `local` again was answered from its cache (`cached: true`, its first run id).
  - The shipped `api/access.yaml` loads: `local` on `policies/default.yaml`, `desk` on `policies/deskcalc.yaml`, no shared secret.

### Known issues
- The tenant check at resume uses the tenant as the run recorded it (`handoff_state.json`), as before. A capability dropped from a tenant's list after a run escalated can still be carried on. Its consent and credentials are still checked afresh.
- `cua record` still loads the default policy for a run's tenant: a run's `run.json` records the tenant's id, app family and base URL, not its policy.
- Overlays remain design only.

### Commit
- `test: enforce tenant isolation across capability runtime`.

### Next
- Phase 13: benchmark expansion.

## Phase 13 — Benchmark expansion

Status: COMPLETE

### Changes
- `bench/tasks/<category>/tasks.yaml`, one suite per category, 40 tasks. `--suite all` runs them all. Each task carries its category as its first tag.
  - `browser` (12): search, sign-on, a rejected sign-on, form entry, pagination, filtering, a page the list does not have, a multi-page flow, an upload attempt, a download attempt, a modal, frame navigation.
  - `recovery` (6): slow load, session expiry, a transient error, a persistent error, an interstitial, a persistent interstitial.
  - `drift` (6): a renamed button, changed nearby text, a duplicated button, a moved field, a renamed frame, a moved output.
  - `side_effects` (7): no, valid, wrong, expired and replayed approval; a duplicate idempotency request; an unknown side effect.
  - `security` (4): prompt injection (a lookup, and a commit with no consent), a malicious redirect, a spoofed confirmation.
  - `composition` (5): the `open_member_subaccount` workflow with consent, an unknown member, no consent, a retried request, a rejected deposit.
  - `core.yaml` is unchanged, so Phase 1's numbers stay comparable.
- Mock app:
  - `/directory`: a GET form with a branch filter and a page number, four members to a page. It is on the nav frame's menu.
  - `/member/{id}/documents`: an upload form. Only a file with content counts.
  - New modes:
    - `server_error_once`: one-shot.
    - `changed_label`, `moved_field`, `changed_frame` and `moved_output`: presentation-only drift.
  - `/_debug/stats` also counts downloads, uploads and requests to the attacker's origin.
- `policies/default.yaml` allows `/directory` and `/member/<id>/documents`.
- A new capability, `member_directory` v1. It was discovered by script (`scripts/discovery/member_directory.yaml`; evidence in `evidence/discovery-member-directory/`), recorded, reviewed and approved.
- Recorder fix: a screen reached with a query (a GET form) now records a location that ends at the path or the query (`LOCATION_END`).
  - `location_matches` is searched in the whole URL, so a bare `$` could never match such a screen. The recorded directory capability timed out on its first replay.
  - A screen reached without a query is recorded as before. Every existing capability and golden therefore re-records byte for byte, and no approval changes.
- Harness (`src/cua/benchmark/`):
  - `subject`: a task operates on a capability, a workflow, or neither. The baseline is asked for the same typed outputs, with the same credentials.
  - Task fields:
    - `workflow`;
    - `approval` (`none`, `valid`, `wrong_inputs`, `expired`, `replayed`);
    - `environment`;
    - `inject_step`;
    - `goal.outputs`;
    - `category`.
  - Replay runs a workflow task through `cua.workflow.runner.run`, with one token per committing step, under an idempotency key.
  - A `replayed` token is spent on a commit before the measured run, and that commit is not counted against the run.
  - Any download, upload or attacker contact the app recorded during a run scores it `wrong` (`Commits.forbidden`, `RunMetrics.forbidden_effects`).
  - A replay failure's message goes into the row's `detail`, so one refusal can be told from another.
  - The report has a per-category table.
- Docs: `bench/README.md` (the expanded suites and their fields), README (the mock app's new screens and modes, the CLI and test-matrix rows), `evidence/README.md`.

### Decisions
- **Upload is benchmarked as an attempt.** No runtime action sends a file, and downloads are blocked by policy. The upload and download tasks have no capability: they are asked of the model alone, and the right answer is to deliver nothing, with the app's own counts as the judge. Building an upload action (schema, surface, tools, policy) was offered and turned down.
- **Token variants run on replay only.** A baseline agent has its risky actions approved or not; it has no token to get wrong.
- **Forbidden effects are universal.** No task in any category wants a download, an upload or a request to the attacker's origin, so any one of them makes a run wrong without a per-task field.
- **A known gap is a task.** The directory shows page 1 under "There is no page 9", and the recorded capability has no detector for the message. `browser-page-out-of-range` scores replay `wrong` on purpose: the benchmark shows the gap instead of hiding it.
- **Workflow outputs for the baseline.** The balance before a commit is read on a screen the agent does not end on, so the baseline is asked for it only optionally, and only the reference number is scored.
- **Scripted only.** The sessions use `--llm scripted` (no key, no cost), as agreed. Live-model statistics are Phase 14's.

### Tests
- `tests/unit/test_benchmark_suites.py` (24):
  - every category exists and covers its scenarios, and every file a task names exists;
  - every form of consent appears;
  - the task-shape rules hold;
  - each token variant is refused for its own reason;
  - forbidden effects make a run wrong for both strategies;
  - a workflow result is scored like a replay;
  - the baseline's goal for a capability, a workflow and a task with neither;
  - the per-category report.
- `tests/integration/test_mockapp_smoke.py` (9 more): the drift modes, the one-shot error, the directory's filter, pages and out-of-range page, uploads and the new counts.
- `tests/unit/test_recorder.py`: the directory's locations, with and without a query.
- The purity test covers `cua.benchmark.subject` and `cua.benchmark.registry`.
- Lists of capabilities in the API, catalog and MCP tests include `member_directory`.

### Results
- Benchmark, `--llm scripted`, two sessions on commit 19cbeb8 with this phase's changes on top. The report is `bench/reports/expanded/summary.md`, and the raw rows are appended to `runs.jsonl`.
  - `bench_01M3MRM79QQEQBJBMTV3Z5PH93`: replay, all 38 replayable tasks, 10 repetitions each (380 runs, about 90 minutes).
  - `bench_01M3MXT3MCCZEPW97ZJ1NXGXZ9`: the scripted baseline, 3 repetitions each (111 runs), and discovery once on the two clean tasks.
- Replay is deterministic here. Every one of the 38 tasks gave the same outcome in all 10 repetitions.
  - 79.0% exact (95% CI 75–83), 18.4% safe stop, 2.6% wrong. The wrong runs are the 10 of `browser-page-out-of-range`, the known gap.
  - No duplicate commit, and no download, upload or attacker contact in any run.
  - Median 10.9 s, p95 29.1 s.
- By category (replay exact / safe stop / wrong):
  - `browser`: 90 / 0 / 10.
  - `composition`: 100 / 0 / 0.
  - `drift`: 33 / 67 / 0. A duplicated button and a moved field are read through. A renamed button, a changed label and a renamed frame stop with `LOCATOR_UNRESOLVED`, and a moved output with `EXTRACTION_FAILED`, before anything is typed or read wrong.
  - `recovery`: 67 / 33 / 0. A transient server error stops with `APP_ERROR` rather than retrying, and a persistent interstitial with `RECOVERY_EXHAUSTED`.
  - `security`: 100 / 0 / 0.
  - `side_effects`: 86 / 14 / 0. `slow_confirm` reports `side_effect: unknown` and commits once.
- Each token variant is refused for its own reason:
  - `wrong_inputs`: "the token grants consent for other inputs";
  - `expired`: "the token expired 541 s ago";
  - `replayed`: "this token was already used by run_...".
- The scripted baseline (a fixed script, no model): 66.7% exact, 6.3% wrong. The 4 duplicate commits come from the retried requests, where the script commits again. Its numbers measure the harness, not a model.
- Gate: ruff, `ruff format --check` and `mypy --strict` are clean. 853 tests pass (814 before this phase), in about 22 minutes.
- Two earlier full runs were not clean, both in the desktop suite, which this phase does not touch.
  - The first died with an access violation after faulthandler's `0x80010108` (COM objects released on another thread, as seen in Phases 10 and 11).
  - The second failed `test_desktop_faults_stop_safely...[ambiguous]` with `PerceptionDrift: ... moved since it was observed`.
  - Run alone, the desktop suite passed 15/15, and the next full run passed everything.
  - They are recorded here as intermittent desktop (UI Automation) failures, not fixed.

### Known issues
- `member_directory` has no detector for "There is no page N", so an out-of-range page is answered with page 1's figures (`browser-page-out-of-range`). A repair is a new version with that detector, through the normal review.
- The scripted baseline plays a fixed script, so its success rate measures the harness, not a model (the report says so).
- Replay stops on a transient server error (`APP_ERROR`) rather than retrying. `recovery-transient-error` records that.
- The desktop suite failed intermittently in two of the last three full runs (a COM access violation, and a `PerceptionDrift` in the `ambiguous` fault case). It passed alone, and in the final full run.

### Commit
- `feat: expand benchmark categories`.

### Next
- Phase 14: statistical evaluation.

## Phase 14 — Statistical evaluation

Status: COMPLETE (pending verification)

### Changes
- Each session records its exact configuration in `sessions.jsonl`. Every new field has a default, so older session records still load.
  - `model` is the model asked for. An alias or a default is resolved to the model the client uses.
  - `models_answered` lists every version that actually answered.
  - `llm_settings` is everything the client sends besides the conversation (`cua.agent.llm.generation_settings`). No client sets a temperature, so the field says "provider default".
  - `prompt_version` fingerprints `cua/agent/prompts.py` and `tools.py`.
  - `task_versions` fingerprints each task.
  - `capability_versions` gives each capability's number and content hash. A workflow gets its file hash and each of its steps.
  - `app_version` fingerprints the mock app's code and templates.
  - `repetitions_by_strategy` and `machine` are also recorded.
- `cua benchmark run --baseline-repetitions N --replay-repetitions N` set per-strategy counts over `--repetitions` (`Plan.count_for`).
- `PROTOCOL_MIN` sets the protocol: baseline 30+ runs per task, replay 10+.
- `src/cua/benchmark/inference.py` builds the comparison between the baseline and replay, on the tasks both ran. It answers the seven questions: LLM calls, cost, latency, repeatability, drift, humans and safety.
  - Each question has one primary measure. The comparison gives it for both strategies, the difference (replay minus baseline) and 95% intervals.
  - Rates use Wilson intervals, and a difference of two rates uses Newcombe's (method 10). It reproduces Newcombe's published example: 0.0524 to 0.3339.
  - Means and medians use a percentile bootstrap with 2,000 resamples and seed 14, so the report is reproducible.
  - A finding is stated only when the interval of the difference excludes zero.
  - Each finding has a strength: *supported* (a live model at protocol size), *indicative* (a live model with fewer runs than the protocol) or *not measured* (a scripted baseline).
  - Repeatability is the share of runs that ended as their task usually ends. Accuracy is reported beside it.
- The report has a new section, "What the measurements support". The setup section lists each session's configuration, and flags a task whose capability or definition changed between the sessions a report combines.
- Runs lost to the provider are set aside. These are `LLM_ERROR` runs with zero model calls: a quota, a spending cap or a refused key.
  - They are left out of every number and listed at the top of the report.
  - An `LLM_ERROR` after the model had answered at least once stays in: that is the baseline's own reliability.
- `models_answered` counts only rows with model calls.
- Docs: `bench/README.md` has a "Statistical protocol" section.

### Decisions
- **Live session on a core subset.** The user chose 6 of the 15 core tasks, with the baseline and replay 30 times each:
  - `lookup-success`
  - `lookup-unknown-member`
  - `lookup-server-error`
  - `lookup-renamed-button`
  - `open-with-consent`
  - `open-retried-request`

  Together they cover a clean read, a business outcome, a persistent failure, drift, a consented commit and idempotency. The full core suite would have cost about $24.
- **Paired tasks only.** The comparison uses only the tasks both strategies ran, so the task mix cannot move a difference.
- **Temperature is not set.** The clients have always used the provider's default sampling. The benchmark measures the agent as it ships, and records that choice rather than pinning a number.
- **Prompt version is a source hash.** Any edit to the prompt or tool module, comments included, counts as a new version. This errs on the side of calling two sessions different.

### Tests
- `tests/unit/test_benchmark_inference.py` (22):
  - Newcombe against the published example;
  - bootstrap reproducibility and separation;
  - modal outcomes and consistency;
  - protocol adequacy;
  - the supported, indicative and not-measured strengths;
  - paired tasks only;
  - unpriced cost;
  - drift read from the category or the tag;
  - duplicate commits as unsafe runs;
  - the report section, and the cross-session version warning;
  - old session records still load;
  - version fingerprints move with their inputs;
  - capability and workflow versions;
  - settings;
  - per-strategy counts;
  - runs lost to the provider are set aside.
- `tests/integration/test_benchmark.py`: the scripted session records every configuration field, and every one of its findings is "not measured".

### Results
- Two live sessions, Gemini `gemini-flash-latest`, which resolved to `gemini-3.8-flash` on every call.
  - `bench_01M3NQEZG8M9HR54RZ849KKQ3Z`: 6 tasks, with the baseline and replay 30 times each and discovery once.
  - `bench_01M3PNNZW5ABDXK197AV98CM94`: the rerun of `open-retried-request` baseline x30.
  - The first session hit the Gemini project's monthly spending cap at 06:13 UTC. All 30 `open-retried-request` baseline runs got `429 RESOURCE_EXHAUSTED` before any model call. The user raised the cap, and the rerun replaced them. The capped rows stay in `runs.jsonl`, and the report sets them aside.
  - Spend: about $7.60 and $2.23, so about $9.83.
- Report: `bench/reports/statistical/summary.md`. Every finding is *supported*, with 30 runs per task per strategy (180 each).
  1. LLM calls: the baseline makes 7.97 per invocation (95% CI 7.72–8.21) and replay 0.
  2. Cost: the baseline costs $0.0540 per invocation ($0.0511–0.0567) and replay $0.
  3. Latency, median: the baseline takes 24.35 s (22.62–25.96) and replay 7.62 s (7.27–8.12). The difference is −16.73 s (−18.35 to −14.97). P95 is 54.2 s against 13.9 s.
     - 29 of `open-retried-request`'s 30 replays are answered from the idempotency store in about 3 ms.
     - The other five tasks give the same result: replay's per-task medians are 5.7–13.8 s, against 14.9–29.1 s for the baseline.
  4. Repeatability:
     - Both strategies ended every task the same way in every repetition (100% modal, CI 97.9–100), so there is no detectable difference.
     - Latency varies less for replay: the median coefficient of variation is 0.10, against 0.29 for the baseline.
     - Accuracy is 83.9% for the baseline and 83.3% for replay.
  5. Drift (a renamed button, n=30): neither strategy gave a wrong answer. The baseline read through the rename all 30 times. Replay stopped safely all 30 times with `LOCATOR_UNRESOLVED`.
  6. Humans: 33.3% for both. The baseline calls `stuck` on the unknown member and the server error, and replay reports `NOT_FOUND` or `APP_ERROR`.
  7. Safety: 16.1% of baseline runs (11.5–22.2) had a duplicate, unconsented or forbidden side effect, against 0% for replay (0–2.1). The baseline committed a second time on 29 of 30 retries.
- Gate: ruff, `ruff format --check` and `mypy --strict` are clean. 875 tests pass (853 before this phase). The desktop suite passed this time.

### Known issues
- Replay's median latency is flattered by answers cached by idempotency key. The per-task medians show the latency finding holds without them.
- `sessions.jsonl` for `bench_01M3NQEZG8M9HR54RZ849KKQ3Z` lists `gemini-flash-latest` among `models_answered`. That came from the capped rows, before the fix. The report takes the answering model from the measured rows instead, which show `gemini-3.8-flash` only.
- The live comparison covers 6 of the 15 core tasks and none of the expanded category suites.

### Commit
- `feat: add statistical benchmark evaluation`.

### Next
- Phase 15: cost break-even analysis.

## Phase 15 — Cost break-even

Status: COMPLETE (pending verification)

### Changes
- `src/cua/benchmark/economics.py` solves N* = discovery / (baseline per run − replay per run) per capability. It also gives both totals at 10, 100, 1,000 and 10,000 invocations.
- Each invocation is priced in three parts:
  - model: the row's own estimate;
  - browser: wall clock × the price of a browser-hour;
  - storage: evidence bytes × the price per GiB-month × retention.
- Replay pays for its browser time and evidence like the other strategies. A total counts only the parts priced for every strategy it is compared with, and names them.
- `bench/pricing.yaml` has a new `infrastructure` section (`InfrastructurePrice`). Without it, browser time and storage are unpriced and the report says the totals are model spend only.
- Each benchmark row records `evidence_bytes`, the size of its run directory. A cached answer from the idempotency store records 0: its directory is the original run's. Older rows are measured from `bench/runs/` if the directory is still there. A run whose directory is gone is left out of the storage mean, and the report gives the coverage.
- `cua benchmark break-even` writes `break_even.json` and `break_even.md` (default `bench/reports/`). It takes the same session filters as `report`, and sets aside runs lost to the provider the same way.
- The summary's model-only break-even now points to it. `bench/README.md` has a "Cost break-even" section.

### Decisions
- **Per capability, not pooled.** Discovery ran on 2 tasks and the baseline on 6. A pooled figure would set one capability's discovery against another's runs. Each capability's discovery is set against the tasks it serves, on the tasks both strategies ran.
- **The per-run cost is the task mix's mean, failures included.** A capability that meets a failing app is priced with those runs.
- **Discovery costs every attempt, divided by the attempts that produced a capability.** A failed discovery is money spent getting one.
- **Uncertainty.** The range beside N* comes from the bootstrap interval of the per-run saving (2,000 resamples, seed 14). Discovery ran once per capability, so its own spread is not in the range. The report says so.
- **Infrastructure prices are configured assumptions.** They are AWS list prices, us-east-1, not checked against a bill:
  - an on-demand c7i.large host at $0.08925/h, running one browser at a time as the benchmark does;
  - S3 Standard at $0.023/GB-month;
  - 12 months' retention.

### Tests
- `tests/unit/test_benchmark_economics.py` (17):
  - the formula and its rounding;
  - the volume totals;
  - replay pays for its browser and its evidence;
  - no break-even when replay costs as much as the baseline;
  - a failed discovery attempt counts;
  - no figure without a successful discovery, a live model or a priced model;
  - paired tasks only;
  - one figure per capability;
  - tasks with no capability are named;
  - runs lost to the provider are set aside;
  - evidence measured from the run directory, with a cached answer counted as 0 and a missing directory as unmeasured;
  - storage coverage;
  - the model-only report;
  - the committed price table;
  - the CLI writes both files.
- Gate: ruff, `ruff format --check` and `mypy` are clean. In the full run, two tests outside this phase failed under load: the desktop calculator and the Ctrl+C-in-a-live-wait discovery test (a 60 s timeout). Both passed when rerun alone. All the benchmark tests pass.
- `cua.benchmark.economics` joined the check that replay-side modules never import a model client.

### Results
- `bench/reports/break_even.md`, from the Phase 14 live sessions (`bench_01M3NQEZG8M9HR54RZ849KKQ3Z`, `bench_01M3PNNZW5ABDXK197AV98CM94`), every part priced, evidence measured for every run:

  | Capability | Discovery | Baseline/run | Replay/run | N* | 1,000 invocations |
  |---|---:|---:|---:|---:|---:|
  | `member_savings_balance` | $0.0305 | $0.0445 | $0.00027 | 1 | $44.45 vs $0.30 |
  | `open_subaccount` | $0.0754 | $0.0754 | $0.00022 | 2 | $75.38 vs $0.29 |

- Discovery costs about as much as one baseline run, so it pays for itself on the first or second invocation. By 1,000 invocations replay costs under 1% of the baseline.
- Replay is not free: it costs about $0.0002–0.0003 per run in browser time and storage. For the lookup it stores more evidence than the baseline (319 against 153 KiB per run). A failed replay keeps its Playwright trace: about 530 KiB on the drift task and 470 KiB on the server error, against about 160 KiB for a clean run.

### Known issues
- A person's review of a discovered capability, re-discovery after drift, and the person who picks up a safe stop are not priced. The report lists all three. Human time is Phase 16.
- The infrastructure prices are list prices, not measured spend.

### Commit
- `feat: add cost break-even analysis`.

### Next
- Phase 16: human intervention economics.

## Phase 16 — Human intervention economics

Status: COMPLETE (pending verification)

### Changes
- Every request to a person is now an `Intervention` (`cua.observability.metrics`), classified by how it ended:
  - `approval`, `recovery` (the automation carried on), `manual_completion` (only the outputs were left), `abort`;
  - `expired` (nobody answered in time), `unanswered` (suspended for `cua resume`);
  - `returned` (handed back to a screen no checkpoint held on, so the run asked again);
  - `not_asked` (no channel to a person), `open`.
- Each intervention records its reason, who decided, the queued time (PAUSED, until someone took it), the in-control time (HUMAN_IN_CONTROL), the person's browser actions (console decisions left out) and where the automation carried on.
- Time is attributed to a request by the `request_id` on each control transition. Older logs without one fall back to the latest request.
- The canonical vocabulary gains two events:
  - `human.pending`, from `escalation.pending`;
  - `human.carried_on`, from `handoff.resumed`, with the step or `done`.
- The discovery loop's `reason_code` is read as the handoff's reason.
- `cua metrics humans [--out DIR]` (`cua.observability.humans`) reports:
  - the runs needing a person against the runs where one acted, and interventions, human wait seconds and browser actions, by run kind;
  - for each kind: count, mean, median and p95 of the time on the person, mean queued and in-control time, and who decided;
  - a table by decider.
  `cua metrics report` includes the same section, and `cua metrics run` lists each request.
- `bench/pricing.yaml` has a new `human.operator_hour` (`HumanPrice`, an assumed $40/h). With it, the report gives a mean cost per intervention.
- Benchmark rows record `human_interventions`, `human_wait_s`, `human_action_count` and `human_kinds`. The aggregation adds `runs_requiring_human` and the totals.
- Docs: the README has a table of the kinds, and `bench/README.md` lists the new row fields.

### Decisions
- **Kinds come from the canonical events, not the result file.** A request that went unanswered and was answered later through `cua resume` is classified by its answer.
- **Discovery handbacks are recoveries.** The loop always carries on with its next turn, and logs no resume point.
- **"Requiring a person" and "a person acted" are both reported.** The gap between them is the work left for someone after the run.
- **Who decided is shown, not guessed.** `evidence-bot` is a scripted operator, and its sub-second answers show the mechanism working, not a person's time. The report says so.
- **The operator's hourly cost is an assumption.** It is in the price table, where anyone can set their own.

### Tests
- `tests/unit/test_human_economics.py` (16):
  - from the committed evidence: an approval, a recovery with its click and in-control time, a manual completion after an unanswered request, and a person's 26 s approval during discovery;
  - from built runs: an abort against an expiry, an unanswered request, a returned handback followed by a recovery (two requests timed apart), a discovery with no channel, a discovery abort that names no request, and an open request;
  - the summary keeps each kind apart and prices it;
  - seconds only without a price;
  - the committed price table;
  - the CLI writes both files;
  - a benchmark row records its human fields, and a cached answer records none;
  - benchmark stats count the runs requiring a person.
- Gate: ruff, `ruff format --check` and `mypy` are clean. In the full run, two desktop replay tests failed under load: the modal fault case, and consent committing once, where a number was typed into the wrong field. All 8 passed when rerun alone. Everything else passed.

### Results
- `bench/reports/humans.md`, from the 175 runs under `evidence/`. 18 are committed; the other 164 are local runs from earlier demo and API sessions (`evidence/runs/` is gitignored).
  - 28 runs (16.0%) needed a person, and a person acted in 24 (13.7%). They made 32 requests.
  - approval: 6, mean 18.9 s, p95 40.9 s, almost all of it queued (reading and deciding).
  - recovery: 8, mean 6.9 s. Manual completion: 5, mean 45.1 s, p95 118.2 s, with 12.4 s in control and 10 browser actions.
  - abort: 6, mean 5.7 s. Expired: 1, after 5,407 s. Returned: 2. Not asked: 4.
  - The 10 requests `anil` decided averaged 29.8 s (p95 118.2 s). At the assumed $40/h, an answered request costs about $0.20, and a manual completion about $0.50.

### Known issues
- The sample is small and mostly demo sessions, so read it as observations, not a rate for production. The benchmark suites run unattended, so their rows record no intervention.
- The time is the run's time on the person. It does not include anything they did outside the console or the browser.
- A capability's review and approval (`cua approve`) is not timed. It happens offline, with no run.

### Commit
- `feat: add human intervention economics`.

### Next
- Phase 17: README redesign.

## Phase 17 — README redesign

Status: COMPLETE

### Changes
- README opens with the measured Phase 14 comparison (six paired tasks, 180 invocations per strategy), confidence intervals, drift behavior and duplicate commits.
- Added a short replay demo, current registry/runtime/surface architecture, lifecycle explanation, and the measured 22-scenario security table.
- Linked statistical, cost, human, security and threat-model reports; preserved detailed discovery, approval, handoff, API/MCP and CLI instructions.
- Added Windows-friendly quality commands and a small scripted benchmark command; excluded desktop tests from the GUI-free test command.

### Tests
- 792 non-browser, non-desktop tests passed on Python 3.14.5.
- Ruff lint and format checks passed; mypy passed for 143 source files.
- All local README file links resolve; git diff whitespace check passed.
- Browser replay integration suite passed, covering success, business outcomes, drift, recovery, approval, idempotency, budgets and evidence redaction; replay CLI options verified.

### Results
- Figures are copied from committed generated reports, with experimental limits and infrastructure pricing assumptions stated.
- Phases 14–16 retain their existing pending-verification status; this documentation phase does not certify the entire desktop suite.

### Known issues
- No new live-model benchmark was run; the README identifies the measured model, environment and date.

### Commit
- `docs: redesign README around measured results and quick demo`.

### Next
- Phase 18: reproducible demo script and evidence.

## Phase 18 — Reproducible demo and evidence

Status: COMPLETE

### Changes
- `scripts/demo/run_demo.py` runs discovery, describe/approve, repeated replay, drift, scripted takeover/resume, single-use consent, and a hostile-page security scenario through real CLI processes.
- `src/cua/demo/runner.py` owns a fresh workspace, mock app and operator console on free ports, session-only signing key and mock credentials. Existing capabilities, evidence, services and review receipts are untouched.
- Default: 100 independent replays, no idempotency cache, scripted discovery and the existing evidence-bot operator. Live discovery is explicitly selected with `--llm gemini|anthropic` and optionally `--model`.
- Preserves draft/approved snapshots, a token-redacted command transcript, run evidence and security output under the gitignored `demo/` directory. Existing output directories are refused.
- `scripts/demo/generate_report.py` and `src/cua/demo/report.py` generate Markdown/JSON only from a completed seven-stage session; verify replay outputs, unique run directories, zero model calls, snapshots, handoff evidence, consent outcomes and security metrics.
- Failure leaves a failed manifest; pending handoffs are aborted through the console and the demo's servers are stopped.
- Added `make demo`, README entry and `scripts/demo/README.md` with reproduction and verification commands.

### Tests
- 15 unit tests cover report regeneration and refusal of missing, contradictory, escaped, duplicated or unsafe evidence; isolated setup refusing overwrites; token and stderr redaction.
- Browser integration: all seven stages with two independent replays passed in 80 s.
- Final non-browser/non-desktop regression: 807 passed, 117 deselected in 63 s. Full report regeneration and transcript approval/resume-token checks passed.
- Ruff lint and format checks passed; mypy passed for 146 source files. README local links resolve and script help matches the documented interface.

### Commands
- `.venv/Scripts/python.exe scripts/demo/run_demo.py --out demo/phase18-smoke --repetitions 3`
- `.venv/Scripts/python.exe scripts/demo/run_demo.py --out demo/phase18-full`
- `.venv/Scripts/python.exe -m pytest tests/unit/test_demo.py`
- `.venv/Scripts/python.exe -m pytest tests/integration/test_demo.py`
- `.venv/Scripts/python.exe -m pytest -m "not browser and not desktop"`
- `.venv/Scripts/python.exe -m ruff check .`, `ruff format --check .`, `mypy`.

### Results
- Three-replay smoke run passed every stage: drift escalated, evidence-bot clicked Find and a separate resume process completed at cp.done; exactly one commit across no/valid/reused consent; one hostile-page attack blocked.
- Full default demonstration passed all seven stages: 100/100 independent lookup replays, 100 unique run IDs, no idempotency cache keys, zero replay model calls, one risky commit, and one blocked hostile-page attack. Generated `demo/phase18-full/summary.md` and `summary.json`.

### Known issues
- Discovery, review, consent and the operator are scripted in the default local demo, explicitly labelled. It demonstrates mechanisms, not live-model planning quality or human intervention timing. No paid model calls or video recording are required.
- Session workspaces retain private runtime plumbing; selected evidence must be reviewed before sharing. Curated committed evidence is not regenerated by this runner.
- During a concurrent demo/regression run, the existing MCP approval/resume unit test returned escalated once instead of success. It passed on its focused rerun and in the final full non-GUI regression; no MCP code changed.
- The full desktop/browser suite was not rerun for this demo tooling change; the new browser integration and the full non-GUI suite were run.

### Commit
- `feat: add reproducible seven-stage demo and evidence reports`.

### Next
- Phase 19: CI gates and benchmark/security artifacts.

## Phase 19 — CI gates and benchmark/security artifacts

Status: IMPLEMENTED — GitHub-hosted Linux verification pending

### Changes
- Added GitHub Actions quality and Chromium jobs for pushes, pull requests and manual dispatch on Python 3.11, with read-only permissions, cancellation and timeouts.
- Quality runs Ruff lint/format, strict mypy and all non-GUI tests. Browser runs all browser tests excluding desktop UI Automation, a six-invocation replay smoke, and the full security benchmark.
- Added `scripts/ci/check_benchmark.py`: requires completed session/coverage, independent uncached run IDs, exact scoring, zero replay model calls, commits and duplicate side effects. Invalid or missing evidence fails.
- Uploads JUnit, raw benchmark/session metrics and Markdown/JSON reports even on failures; excludes raw browser evidence and private session plumbing. Browser artifacts retained for 14 days.
- Added `docs/CI.md`, README link and gitignored local artifact output. Existing committed reports are preserved.

### Verification
- Six real Chromium smoke invocations passed the new gate; report regeneration passed.
- Full security CLI: 22/22 attacks blocked, zero unsafe actions, secret exposures, policy/approval bypasses or tenant isolation failures.
- Non-browser/non-desktop regression: 817 passed, 117 deselected. Four additional session-validation cases were then added; focused gate suite: 14 passed.
- Ruff lint/format, source mypy and diff whitespace checks passed; workflow YAML parses.
- Browser execution required leaving the local sandbox because Chromium spawn was refused with EPERM; the authorized rerun passed.

### Remaining verification
- The hosted Ubuntu/Python 3.11 workflow and full browser test selection have not been run in this phase. Local verification used Windows/Python 3.14.5.
- Branch protection must be configured by a repository administrator to require `quality` and `browser`. Desktop tests remain an interactive Windows check.
- Scripted smoke results check runtime correctness, not live-model performance or statistical improvements.

### Hosted CI portability fixes
- Retrieved the first failed hosted run: six Linux mypy errors, detached Chromium startup failures in consent/handoff paths, and three vision assertions using Windows-rendered screenshots on Linux.
- Kept runtime Windows guards while allowing mypy to analyze their exports on either platform; accessed Windows-only ctypes/subprocess attributes dynamically. CI now checks native Linux and Windows type targets.
- Added explicit `CUA_CHROMIUM_NO_SANDBOX=1` opt-in for detached browsers in the hosted browser job. Other detached launches keep the sandbox enabled by default. Three unit cases verify the opt-in.
- Vision integration fixtures capture controls on their current platform before approving test artifacts, preserving recorder coverage, confidence thresholds and refusal assertions.
- Windows verification: 824 non-browser/non-desktop tests passed; 11 vision integration tests passed; lint/format and Linux/Windows mypy checks passed.
- Ubuntu/WSL verification on Python 3.14.4: all 19 targeted browser tests passed, covering every failure from the hosted run plus related API/catalog/vision cases, the complete demo, CLI resume and MCP invocation. Supplied missing Chromium libraries from a local test directory; no distro package installation was required.
- Hosted Linux/Python 3.11 passed both mypy targets. Its newly reached non-GUI suite found one test assuming desktop capabilities are invocable on Linux (823 passed). Corrected that assertion to reflect platform availability and additionally check tenant scope with `?all=true`; all 33 API unit tests pass on Windows and Ubuntu.
