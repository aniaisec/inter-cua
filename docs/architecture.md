# Architecture and runtime invariants

## Runtime architecture

```text
Agent / CLI / HTTP / MCP
          |
          v
Capability registry -- typed inputs, approved versions, lifecycle
          |
          v
Runtime -- tenant policy, consent, idempotency, budgets, workflows
          |
          v
Deterministic replay -- checkpoints, recovery, human handoff
          |
          +--> Playwright browser
          +--> Windows UI Automation
          +--> Optional vision candidates, validated before action
          |
          v
Evidence --> metrics / capability health / drift --> reviewed candidates
```

Discovery is the model-driven entry point that creates a draft. A person
describes and approves its exact content before unattended execution. The
registry retains versions and enforces deprecation and revocation; a drift
repair becomes another draft, evaluated and reviewed before approval.

## Modules

The reasoning behind each choice is in [REPORT.md](../REPORT.md). This is the
map of the code.

```
mockapp/            the automation target
deskapp/            the desktop target: DeskCalc, a WinForms window (PowerShell)
src/cua/surface/    Surface protocol, a11y perception, locator ladder, conditions, Playwright adapter
src/cua/surface/windows/  the Windows UI Automation adapter
src/cua/surface/vision/   the vision fallback: detector, candidate, validator
src/cua/agent/      discovery loop, prompts, tools, stopping conditions, LLM clients (Claude, Gemini, scripted)
src/cua/artifact/   capability schema, recorder, store (versioning, content seal), describe
src/cua/registry/   approved versions, lifecycle ledger and tenant-scoped resolution
src/cua/workflow/   typed bindings, preflight, journals and replay composition
src/cua/drift/      evidence classification, candidate proposals and evaluation
src/cua/api/        HTTP authentication, authorization and shared run service
src/cua/mcp/        stdio tools using the same run service
src/cua/demo/       isolated seven-stage runner and evidence report
src/cua/replay/     engine, waits, detectors, recoverers, resume-state search, result contract, runner
src/cua/policy/     allowlist and risk, approval gate, approval tokens, redaction
src/cua/secrets/    secret:// resolver (environment or file, memory only)
src/cua/escalation/ control lease, state machine, intervention requests, operator console, human-action capture
src/cua/evidence/   run log, trace, `make evidence` builder
src/cua/observability/ canonical events read from run directories, run metrics, capability health, cost
src/cua/benchmark/  repeated-LLM baseline vs discover-then-replay (`bench/`)
policies/           allowlist, risk rules, masks and scrub patterns
tenants/            per-deployment binding: base_url, policy, the capabilities it runs, secret refs
capabilities/       approved capabilities, the app-family template, the exported JSON Schema
evidence/           discovery, replay and escalation runs
tests/              unit | integration (`-m browser` needs Chromium) | desktop (`-m desktop`, Windows)
```

### Surface

Everything that touches the target goes through `Surface`, so nothing above it
knows the target is a web page.

- **Perception** takes one `aria_snapshot()` per frame and flattens it into
  refs, roles, names, values, boxes and parents. Refs belong to one
  observation and are never reused, so a stale ref addresses nothing rather
  than whatever took its place. `python -m cua.surface --url
  http://127.0.0.1:8000/login --signed-in` prints what the automation sees.
- **The locator ladder** names a control by `role_name`, then `near_text`,
  then `table_cell`, then `bbox`. Each rung must resolve to exactly one node;
  a rung that matches nothing or several falls through, and the run records
  which rung answered. Pixels are refused outside the viewport the capability
  was recorded in, and are never acted on unattended.
- **Conditions** (`location_matches`, `region_present`, `text_present`,
  `value_set`, `error_banner_present`, `validation_message_present`, ...) are
  pure functions of an observation, and each documents what it would mean on
  a desktop surface.
- **Desktop.** `WindowsSurface` drives a Windows application through UI
  Automation with the same protocol: UIA control types become the same roles,
  a window becomes the same `Observation` (location
  `uia://<app>/<window title>`, boxes from the window's corner), and refs are
  just as observation-scoped. Input is posted, never invoked: a button's
  `Invoke` does not return while the message box it opened is up, and the
  surface must be free to answer that box, as the web answers a `confirm`.
  The tenant says how the application starts (`tenants/desk.yaml`); the
  capability names only its location.
- **Compatibility.** A surface publishes a `descriptor`: adapter name,
  contract version and features from a closed vocabulary (`frames`,
  `geometry`, `fixed_viewport`, `forms`, `dialogs`, `screenshots`, `pointer`,
  `egress_control`, `session_handoff`, ...; `src/cua/surface/features.py`). A
  capability needs the features its content uses: a `near_text` rung needs
  `geometry`, a rung scoped to frame `main` needs `frames`. Replay compares the
  two twice, against the registered adapter before a browser starts and
  against the surface it was actually handed before the first action. A
  capability short of a feature ends as `failure SURFACE_INCOMPATIBLE`,
  `side_effect: none`, and is not offered by `cua catalog` or run by a
  workflow.

- **Vision, as a fallback.** When every rung of a click's ladder misses (the
  control is drawn but the tree no longer has it), `cua replay --vision`
  looks for the control's recorded picture on a masked screenshot. The
  picture is a crop `cua record` cut from the discovery screenshot, stored in
  the capability and approved with it (schema 1.3 `appearance`). The detector
  is template matching, with no model, and it returns candidates, never
  clicks. The validator accepts one only when it is a click on a `safe` step,
  there is exactly one clear match (0.97, and 0.1 ahead of any other), the
  match is on screen and clear of every mask, the tree has no other control
  at that point, and the policy allows it with no risky rule covering the
  screen. The click then carries a hash of the pixels it was chosen on; the
  surface looks again and refuses if they changed. After that the step's own
  expectation and checkpoint decide, as for any step. Anything refused is
  the locator failure it already was, escalated. The rung used is `vision`,
  and drift reports it as `CONTROL_UNLABELED`. The policy must allow it
  (`vision.allowed`) as well as the run.

No CSS or XPath selector appears under `src/cua`. The one exception is the
document-level `body` anchor `aria_snapshot` requires, which never names a
control.

### Capability

The pydantic model in `src/cua/artifact/schema.py` is the source of truth;
`capabilities/schema/capability-1.3.json` is exported from it. A capability
carries its contract (`inputs`, `outputs`, `contract` with side effects,
idempotency and declared business outcomes, and `credentials` as `secret://`
refs), its `steps` (a locator ladder, `risk`, `approval`, `retry`, and what
must hold afterwards), compound `checkpoints` that bind the inputs,
`outcome_detectors` and `recoverers` from the app-family template
(`capabilities/families/legacy-core.yaml`), and `provenance` naming the
discovery run. Any change bumps the version and resets it to draft, including
a hand edit, which the content seal written on every save catches.

Schema 1.2 adds `surface_requirements`: the surface features the capability
needs, written by `cua record` and checked on load to cover every feature its
content uses. A 1.1 capability is read as it is, with its hash and approval
unchanged; its requirements are derived from its content, and `cua describe`
and `cua surfaces` say so.

Schema 1.3 adds `appearance` to a click step: the control's recorded picture,
for the vision fallback. It is content, so it is hashed and approved with the
rest, and `cua describe` lists it as the last rung. A 1.1 or 1.2 capability
has none, keeps its hash, and has no vision fallback.

### Registry

`capabilities/<name>.json` is the working copy that `cua record` writes and
`cua describe` reads. `cua approve` also keeps a sealed copy of the version it
approves under `capabilities/registry/<name>/v<N>.json`, so an approved version
keeps running after the capability is re-recorded: versions coexist, and a
call by name runs the highest approved one. A version's status is its
artifact's own approval with `capabilities/registry/lifecycle.jsonl` applied on
top: every change after review, by whom, when and why, appended and never
rewritten.

```
draft --describe--> review --approve--> approved --deprecate--> deprecated
                                          |       <--reinstate--    |
                                          \--revoke--> revoked <--revoke--/
```

A deprecated version runs only when it is named (`--version`, or its path),
with a warning; a revoked one starts nothing, and is final. The lifecycle is
checked when an execution starts: a run already under way when its version is
revoked finishes (stopping it between a commit and its confirmation would
leave the caller not knowing whether it happened), a retry answered from the
idempotency cache still gets its stored answer, and `cua resume` refuses to
carry on a paused run of a revoked version. A ledger that cannot be read
refuses every run rather than guessing. Health (`cua registry health`) is only
ever what the replays on record show; a version with none has no health, not a
good one.

### Drift

Drift is the app no longer matching what a capability recorded: a control
under another name, in another place or frame, or duplicated; a checkpoint or
an output that no longer holds. `src/cua/drift/` reads it out of run
directories, like the canonical events it is built on: each control a step
looked up (the rung it was recorded on, and what every rung found), each one
that could not be named, and the screen kept at the moment of failure. Each
`DriftEvent` names the capability version, tenant, app family, step, the rung
it was recorded on, what each rung found, whether the run survived it, the
evidence file, and what the classification rests on. `cua drift report` gives
drift events per invocation by capability version and tenant, and per lookup
by the rung a control was recorded on; drift injected into the mock app on
purpose is counted and shown beside the rest.

A drift that stopped a run can become a **candidate**, never a deployment:

```
v3 approved -> drift -> cua drift propose -> v4 candidate (draft)
            -> cua drift evaluate (benchmark + security checks)
            -> cua describe + cua approve (a person) -> v4 approved, the new default
```

Only `CONTROL_RENAMED` is repaired from the evidence, with no model: the kept
failure screen shows exactly one control of the recorded role where the
recorded one was, so the repair names it (`role_name` "Find") in front of the
recorded rungs, which stay, so a tenant still on the old screen is served.
Any other kind is reported with a pointer to re-record. The candidate is
written under `capabilities/candidates/<name>/v<N>/` (`capability.json`,
`candidate.json`, `rationale.md`, `evidence/`) and nothing else is touched:
the working copy, the registered versions and the ledger stay as they were,
and the version that drifted stays the default. Static checks hold it to the
narrowest change (only that step's ladder differs; the new ladder names the
same control on the failure screen by a trusted rung; no new text the policy
scrubs; a committing step is flagged for review). `cua drift evaluate` replays
the benchmark tasks for that capability with the candidate and with the
version it repairs, in a fresh mock app, and gates on: the drift task answered,
no task worse than before, no wrong answer or unexpected commit, security
tasks still refused. `cua approve` refuses a candidate until an evaluation has
passed on exactly its content, and never after it was rejected.

### Workflows

A workflow (`workflows/<name>.yaml`) composes approved capabilities into one
typed request, so an agent calls "look the member up, then open a sub-account"
instead of discovering the whole flow again:

```yaml
steps:
  - id: lookup
    capability: member_savings_balance
    inputs: {member_id: "${member_id}"}
  - id: open
    capability: open_subaccount
    inputs: {member_id: "${member_id}", initial_deposit: "${initial_deposit}"}
outputs:
  member_name: ${lookup.output.member_name}
  reference_number: ${open.output.reference_number}
```

A binding is a workflow input (`${member_id}`), an earlier step's output
(`${lookup.output.savings_balance}`) or a literal, never a template. Each step
resolves in the registry like a call by name (the highest approved version,
or a pinned one), and before anything runs `src/cua/workflow/` checks every
reference, every type (a value flows only into an input of its type, or an
integer into a decimal), that no optional value feeds a required input, that
a sensitive value never lands where it would be logged in the clear, and that
every step may run on this tenant. A wrong definition is a usage error; a step
that is a draft, revoked or for another app family refuses the whole workflow,
so no earlier step runs for nothing.

Every step then runs through `replay`, the same entry point as `cua replay`:
its approval gate, policy, lifecycle check, consent, budget and idempotency
apply unchanged, because the workflow never goes around them. What it adds:

- the first step that does not succeed ends the workflow and gives it its kind:
  a lookup that answers `NOT_FOUND` means nothing is opened;
- consent is per committing step (`--approval open=<token>`, from `cua workflow
  approval-token`), bound like any token to that capability, tenant and the
  exact inputs the step will get, and checked before the first step runs. With
  `--handoff` and no token, the step escalates to a person instead;
- a workflow with a committing step needs an idempotency key. Each step runs
  under a key derived from it, so a retry is answered step by step from
  replay's own cache and a commit is never made twice; the same key for other
  inputs is `INPUT_INVALID`. A retry runs the versions the first attempt
  resolved. An escalated step is never started again: once `cua resume` has
  finished it, running the workflow again with the same key carries on from
  its answer. A committing step whose process was killed mid-run is not
  started again either; without a result it reports `INTERRUPTED` with
  `side_effect: unknown`;
- `timeout_s` is for the whole workflow; each step gets what is left.

The result has replay's four kinds, the folded `side_effect` (`unknown`, else
`committed`, else `none`), the typed outputs, and every step's kind, run id and
side effect. Each run is recorded in `<runs dir>/workflows/<wf id>/`
(`workflow.json` with the resolved plan and the request, sensitive inputs
masked and tokens by hash; `log.jsonl`; `result.json`); each step's run
directory is an ordinary replay run.

### Replay

Per step: find the control (exactly one node, on a rung it trusts), check the
policy, act, then wait by condition until an in-scope detector fires (hard,
then business, then recoverable) or the step's expectation and checkpoint
hold. Recovery is bounded per step and per run. An irreversible step is never
repeated; if it does not visibly land, its marker decides between
`side_effect: committed` and `unknown`. A failed run keeps a scrubbed
Playwright `trace.zip` beside its log and masked screenshots.

### Safety

`policies/default.yaml` is checked before every action by discovery and replay
alike: allowed origins, paths and actions; `blocked_actions` (external
navigation, downloads), judged from a link's destination before it is clicked;
`risky_rules` that need a signed approval token or a person; masks applied when
a screenshot is taken; and SSN and account-number shapes scrubbed from
everything written.

### Security

The screens are hostile and the model is steerable, so nothing depends on
the model refusing an injected instruction ([docs/THREAT_MODEL.md](THREAT_MODEL.md),
[SECURITY.md](../SECURITY.md)). On top of the policy above:

- **Credential sink.** A credential placeholder is substituted only in a text
  field on a sign-on screen (`credential_paths`), and a password only into a
  field labelled as one. A screen that asks to "re-enter your password", or a
  capability changed to type it into a search box, is refused before the
  value exists anywhere.
- **Egress guard.** The browser aborts every request to an origin the policy
  does not allow: a form that posts elsewhere, a beacon, and each hop of a
  redirect (Playwright's `route` alone misses redirect hops; CDP
  interception catches them).
- **Approval on record.** A capability file that says it is approved runs
  only if the registry ledger records that approval for its exact content.
  The seal is a hash anyone can recompute, and it is not treated as
  approval.
- **Tenant isolation.** Every invocation takes its deployment, credentials,
  policy and the capabilities it may run from its tenant file
  (`tenants/<id>.yaml`) alone. A capability's credential must be its own
  tenant's (`secret://{tenant.id}/...`) and never a system secret (`cua/`).
  Approval tokens are signed with a key per tenant and name their tenant.
  Idempotency results, workflow journals and the operator console are keyed
  by tenant, so a key or request id one tenant's caller holds opens nothing
  of another's.
- **Screen content is labelled** as the application's in the prompt. This is
  defense in depth; no test relies on it.

`cua security run` stages 22 attacks across 15 threats, 11 live against a
fresh mock app and 11 offline. The "model" is a script that follows every
injected instruction. Each attack is judged by its effects: requests that
reached the attacker's origin, files served, commits, and where a canary
password turned up. The latest report is
[bench/security/reports/summary.md](../bench/security/reports/summary.md). The
negative controls in `tests/security/` switch the egress guard and the
credential sink off and show the same attacks then succeed.

### Handoff

With `--handoff`, a fault a person could fix writes an intervention request
(capability, step, reason, expected, a scrubbed excerpt of the screen, a
masked screenshot, the DevTools endpoint of the live browser), gives up the
controls and waits, with its timers stopped. Who holds the controls is one
record per run (`control.json`), and every change of hands is a line of
`state_transitions.jsonl`:

```
AUTOMATION -> PAUSED -> HUMAN_IN_CONTROL -> RESUMING -> AUTOMATION
                 |              |               \-> PAUSED   (nothing holds: ask again)
                 \-> ABORTED    \-> ABORTED
```

While a person holds the controls the console attaches to the same browser
over CDP and records clicks, the fields they edit themselves (which control
and how many characters, never the value), key presses and navigations in
`human_actions.jsonl`. When
they hand back, the run tests its checkpoints newest first and carries on
after the newest that holds. A caller is never blocked on a person:
`--handoff-wait 0`, or nobody answering in time, returns `escalated` with a
`resume_token`, and the browser outlives the process for `cua resume`.

### Observability

A run directory is the record: `log.jsonl`, `model_calls.jsonl`,
`human_actions.jsonl`, `state_transitions.jsonl`, `result.json`.
`src/cua/observability/` reads those files into one canonical vocabulary
(`run.*`, `step.*`, `llm.*`, `locator.*`, `recovery.*`, `policy.*`, `human.*`,
`side_effect.*`), each event carrying `run_id`, `invocation_id` (a request
retried under one idempotency key is one invocation), tenant, capability id and
version, and step, and naming the source line it came from. Events are derived,
never written beside the log, so old evidence reads the same as new and there
is no second writer to disagree with the first; nothing is carried that a
question does not need (no typed text, no query strings).

`cua metrics run` splits a run's wall clock into parts that add up to it:
`human`, `llm`, `recovery`, `act`, `locate`, `verify` (settle and checkpoint),
`evidence` (the observation and screenshot kept after each step), `startup` and
`other`. Model cost is priced per call from `bench/pricing.yaml`; an unknown
model is *unpriced*, never free, and every figure is an estimate, not billing
data. Capability health leaves out runs with an injected fault and runs refused
for want of an approval (counted, not rated), and is kept per version.

`cua metrics humans [--out DIR]` reports what people spent on the runs. It
takes every request to a person and says how it ended:

| Ended as | Meaning |
|---|---|
| `approval` | a person approved a step that commits a change |
| `recovery` | a person handed back and the automation carried on |
| `manual_completion` | a person handed back with only the outputs left to read |
| `abort` | a person stopped the run |
| `expired` | nobody answered before the request expired |
| `unanswered` | nobody took it while the process waited; the run is suspended for `cua resume` |
| `returned` | handed back to a screen no checkpoint held on, so the run asked again |
| `not_asked` | the run needed a person and had no channel to one |
| `open` | the run ended with the request still open |

Each kind gets its count, and the mean, median and p95 of the run's time on the
person. That time is split into *queued* (until someone took the request) and
*in control* (while they held it). The report also gives the browser actions,
who decided, the share of runs that needed a person against the share where one
acted, and a cost per intervention at the `human.operator_hour` in
`bench/pricing.yaml` (an assumption). `cua metrics run` lists the same per
request, and benchmark rows record `human_interventions`, `human_wait_s`,
`human_action_count` and `human_kinds`.

[Documentation index](index.md) · [Project README](../README.md)
