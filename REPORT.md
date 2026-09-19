# REPORT

inter-cua turns one LLM-driven run through a legacy UI into a capability an
agent can call: a typed contract plus the steps to carry it out, replayed with
no model in the process. When replay cannot safely go on, a person takes over
the same browser session and hands it back. The target is a local mock of a
legacy credit-union core, hostile on purpose (framesets, nested tables, no
ids, inputs with no accessible name), with thirteen failure modes that can be
switched on per request. Both discovery runs in `evidence/` are real model
runs.

## 1. Architecture

```
 cua discover (LLM loop) ─► run dir ─► recorder ─► capability.json ─► describe / approve
          │                                               │
          ▼                                               ▼
 Surface: a11y tree, locator ladder, conditions ◄── cua replay (no model) ─► ReplayResult
 (Playwright; policy checked before every action)         │ intervention request (file)
          ▲                                               ▼
          └────────── CDP, same browser ────────── cua operator (control lease, resume)
```

Discovery or replay runs in one Python process. The operator console runs in
a second one. The two meet only through files and the browser's CDP endpoint.
Module boundaries do the job services would otherwise do: `replay/` and
`artifact/` import nothing from `agent/` or any model SDK, and a test imports
all of replay in a clean interpreter to check that no model client was loaded.

| Decision | Choice | Why |
|---|---|---|
| Runtime | Python 3.11, pydantic v2, Playwright, FastAPI; `mypy --strict` | Typed schemas with a JSON Schema export for free. Playwright gives the accessibility tree, screenshots, coordinate clicks and CDP in one API |
| Target | Local mock core | Every failure class in the brief can be injected on demand. No terms-of-service or PII risk, and reviewers can run it offline |
| Perception | Accessibility tree first, masked screenshot second; no CSS or XPath | Works when the markup is hostile, and desktop platforms have the same kind of tree (UIA, AX) |
| LLM | Provider-neutral: Claude (`claude-sonnet-5` default) or Gemini, plus a scripted client for running without a key. One typed tool call per turn (`click`, `type`, `press`, `read`, `done`, `stuck`) | Typed actions, so there is nothing to parse. The evidence runs used Gemini (`gemini-flash-latest`, which answered as `gemini-3.8-flash`) because that is the key I had. Goal 1 took 6 calls, goal 2 took 10 |
| Agent loop | Observe (compact tree + masked screenshot) → decide → policy check → act. Stops at 30 steps, after 10 min, after 3 unchanged screens, or on `stuck` | Policy is enforced from the first action. `done` must name a ref for every declared output, and the runner reads each value itself |
| Live session | Chromium as its own process with a CDP endpoint; a control lease | A real handoff on the same browser. Remotely, the same endpoint would sit behind auth and a viewer |
| Queue | Files: requests, `control.json`, `state_transitions.jsonl` | Enough for one machine, and easy to inspect. The request queue would be the first service boundary |

## 2. Artifact schema

A capability (`src/cua/artifact/schema.py`, exported to
`capabilities/schema/capability-1.1.json`) is a contract first and a step list
second.

- **`contract`** tells a calling agent, before it calls: `side_effects`,
  `idempotent`, `may_escalate`, and the declared business `outcomes` with
  their payload shapes.
- **`inputs`** hold only what the agent supplies. **`credentials`** are
  separate: an opaque `secret://{tenant.id}/mockcore/operator` ref that the
  runner resolves into memory. The agent-facing contract carries no secrets,
  and the artifact names no deployment.
- **`outputs`** are typed and found by position (a table row and column, or
  the label beside the value), never by the value that was read.
- **`steps`** name each control with a **locator ladder**: `role_name` →
  `near_text` → `table_cell` → `bbox`. Each rung must resolve to *exactly
  one* node. An ambiguous rung falls through rather than guessing, and a slip
  down the ladder is a signal instead of a silent wrong click. `bbox` carries
  `recording_env` and is kept for reference only: replay never clicks by pixel
  unattended.
- **`risk`, `approval`, `retry.allowed`, `side_effect_marker`** are what stop
  an irreversible step from being pressed twice. Validation refuses a
  retryable irreversible step.
- **`checkpoints`** are compound and bind the inputs. "On member detail"
  means the right location, the Member Detail region, *and* `${member_id}` on
  screen.
- **`outcome_detectors`** are data. Each has a `class` (hard, business,
  recoverable) that maps onto the result kinds, a `scope` (so session expiry
  cannot fire while the run is still signing on), and a fixed precedence.
  They come from an app-family template: one happy-path run cannot learn
  what the product does when things go wrong, and a tenant can reword a
  detector without touching Python.
- **Conditions are surface-neutral** (`location_matches`, `region_present`,
  `error_banner_present`). The surface adapter decides what "location" means.
- **`version`, `approval_state`, `content_sha256`.** Any change bumps the
  version and resets approval to draft, including a hand edit, which the seal
  catches. `cua describe` renders the capability in plain words and records
  what it showed; `cua approve` refuses unless the capability is still
  exactly that.
- **`provenance`** names the discovery run, the model, a transcript hash (the
  transcript stays in `evidence/`), and the rung each step was recorded on.
  That last item is the baseline drift is measured against.

The recorder templates a typed value as `${param}` only when it equals a
declared `--param` value, and it refuses a run in which a secret was typed
literally.

## 3. Determinism & error handling

For each step, replay does the following. It resolves the ladder to exactly
one node, waits for the step's precondition, checks the policy, and acts.
Then it waits *by condition* (there are no sleeps) until something decides
the step: an in-scope detector, checked in the order hard → business →
recoverable, or else the step's `expect_after` and its checkpoint. Given the
same inputs, it takes the same path on the same rungs, and a test runs it
three times to show that.

| Kind | Exit | Carries | Examples |
|---|---|---|---|
| `success` | 0 | typed `outputs` | balance; reference number |
| `business_outcome` | 2 | code, payload, any outputs already read | `NOT_FOUND`, `PERMISSION_DENIED`, `VALIDATION_ERROR {field}` |
| `failure` | 1 | `step_id`, `expected`, `observed` (scrubbed tree excerpt), screenshot, `trace.zip`, **`side_effect`** | 13 closed codes, e.g. `LOCATOR_UNRESOLVED`, `APP_ERROR`, `AUTH_FAILED`, `TIMEOUT`, `RECOVERY_EXHAUSTED`, `INPUT_INVALID` |
| `escalated` | 3 | `request_id`, `resume_token` | `STUCK`, `NEEDS_APPROVAL`, `UNRECOVERABLE`, `DEAD_END` |

All four kinds also carry `recoveries[]`, `handoffs[]`, `locator_rungs_used`,
and `warnings`.

**Recoverable conditions are handled, reported and bounded.** A system notice
is dismissed. A slow screen is checked again before the step is repeated, and
the step is repeated only if it allows retry. An expired session signs on
again and resumes after the newest checkpoint that still holds. Recovery is
capped per step and per run, so a notice that keeps coming back ends as
`RECOVERY_EXHAUSTED`.

**Irreversible steps are never retried.** If a commit does not visibly land in
time, its marker decides between `side_effect: committed` and `unknown`.
`unknown` is its own state because the right response is neither retry (which
could commit twice) nor giving up (which could leave a change nobody knows
about). The right response is to find out. The `replay-slow-confirm` evidence
shows exactly one Confirm request reaching the app.

**Judgment calls.**
- `PERMISSION_DENIED` is a business outcome: the caller needs to know, and no
  one can fix it mid-run.
- On a step timeout, every business detector is checked regardless of scope,
  because scope sets precedence, not deafness.
- Before any browser starts, replay refuses a draft (`POLICY_BLOCKED`),
  validates the inputs (`INPUT_INVALID`), verifies the approval token, and
  answers a repeated `idempotency_key` from the cache. An `escalated` result
  is never cached.

**Drift.** A rung that answers below the one recorded in `provenance` adds a
`locator.slip` warning. An exhausted ladder is `LOCATOR_UNRESOLVED`, and with
`--handoff` it goes to a person. `renamed_button` stands in for this case.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `Surface` (`observe`, `act`, `resolve`,
`wait_for`, `evaluate`, `expose`) together with the locator strategies and the
condition vocabulary. A capability names neither a DOM nor a URL. Its entry is
`{tenant.base_url}/login`, its controls are roles, names and nearby labels,
and its conditions are surface-neutral.

- **Legacy web** changes nothing. The mock app is a legacy web app, and it
  still has an accessibility tree.
- **Desktop** swaps `PlaywrightSurface` for a UIA or AX surface. Control
  types map to roles, `location_matches` reads the window title, and
  `region_present` looks for a named pane. `target.surface` selects the
  adapter.
- **The limit** is a surface with no tree, such as a canvas or a Citrix/RDP
  session. Only a vision-grounded `bbox` rung would work there, and I cut it.

**Multi-tenant reuse.** A capability is keyed by `target.app_family`, not by
tenant. `tenants/<id>.yaml` binds everything that belongs to one deployment:
`base_url`, the store behind each `secret://` ref, and an optional overlay.
The policy is written against `{tenant.base_url}` for the same reason. An
overlay overrides only what really differs between installs of the same
vendor product, and it is versioned separately:

```yaml
# capabilities/overlays/tenant-fcu-042.yaml (design only, not built)
applies_to: {app_family: legacy-core, capability: member_savings_balance, version: ">=3"}
target: {entry: {kind: location, pattern: "https://core.fcu042.example/portal/login"}, version_hint: "1.3"}
step_overrides:
  search.submit: {target: [{strategy: role_name, role: button, name: "Find"}]}
detector_overrides:
  NOT_FOUND: {match: {kind: text_present, text: "Member does not exist"}}
```

**Drift detection.** Every `ReplayResult` already reports
`locator_rungs_used`, recoveries, and any failed checkpoint. Rolled up per
tenant, app family and `version_hint`, these fields are the drift signal:

- A slip from `role_name` to `near_text` is a warning.
- A slip to `bbox`, an exhausted ladder, or a checkpoint failure rate above
  baseline opens an intervention.
- The fix is a new overlay version for that tenant, not a re-record.
- When many tenants slip the same way, the vendor has shipped an upgrade, and
  the base capability gets a new version.

## 5. Escalation & handoff

**Detecting "stuck".**
- In discovery: the agent calls `stuck`, three unchanged screens pass (a dead
  end), or a risky action has no approval.
- In replay: an exhausted ladder (`STUCK`), a risky step with no token
  (`NEEDS_APPROVAL`), exhausted recovery, a hard detector, or a commit whose
  side effect is `unknown` (`UNRECOVERABLE`).

Handoff is opt-in (`--handoff`). Without it, each of these is a `failure`
that names the escalation it replaced, so an unattended caller gets a fast,
typed answer. `allow_escalation=false` turns them into `ESCALATION_ABORTED`
without asking anyone.

**Routing.** An intervention request carries the capability, step, reason,
the fault, what was expected, a scrubbed tree excerpt, a masked screenshot,
and the CDP endpoint and page of the live browser. The operator console
(`cua operator`) lists open requests and offers *Take control*, *Resume*
(optionally naming where), *Retry step*, *Approve action* and *Abort*. The
page is deliberately minimal. The mechanism behind it is not.

**Control transfer.** Each run has one `ControlLease{holder: automation |
human | none}`, driven by the state machine `AUTOMATION → PAUSED →
HUMAN_IN_CONTROL → RESUMING → AUTOMATION`, or `ABORTED`. Illegal transitions
raise. Every action the automation takes checks the lease first, so it never
acts over a person. Observing needs no lease, which is how the automation
takes the request screenshot and later reads what the person left. Step
timers and the budget clock stop while a person holds the lease. Every change
of hands is written to `state_transitions.jsonl`.

**Same live session.** Chromium runs as its own process and replay attaches
to it over CDP. The person works in that window, or through the DevTools link
in the request if it is headless. The console attaches to the same browser
and records into `human_actions.jsonl`: clicks, changes (which control and how
many characters, never the value), key presses and navigations. The person's
actions are recorded, not policy-checked: they are the escalation path for
what the policy did not foresee.

**Handing back.** The run takes nobody's word for where it is. It reads
whatever outputs are on screen, tests its checkpoints newest first, and
carries on after the newest one that holds. An operator's `resume_at` only
narrows the search. The run never goes back before a commit that happened or
may have happened, and if the person committed, the result says so. If no
checkpoint holds, it asks again. The caller is never blocked: if nobody takes
the request in time, replay returns `escalated` with a `resume_token`, the
browser outlives the process, and `cua resume <token>` finishes the run. For
`NEEDS_APPROVAL` the caller may pass `resume` a fresh approval token instead of
using the console; it is checked as replay checks one, and a refusal leaves the
run waiting. In
the goal-2 discovery run, a person approved the Confirm step by hand through
this console.

## 6. Safety

- **Allowlist** (`policies/default.yaml`), checked before every action in
  discovery and in replay: origins, URL paths, action types, and blocked
  actions. External navigation and downloads are judged from a link's
  destination *before* the click, and the browser refuses downloads anyway.
  The agent is told why an action was blocked, and carries on.
- **Risky actions need a token or a person.** Risk rules (button name plus
  screen) mark actions that commit something. A hard block would make
  `open_subaccount` useless. Escalating every time would make unattended
  calls useless. So a caller that already holds consent passes a token.
  - The token is HMAC-signed with the tenant's key and bound to the
    capability version and content hash, the tenant, a hash of the inputs, the
    approver, and an expiry. It is spent on the first commit.
  - Without a token, the action goes to a person or fails.
  - An approval never lifts a block.
  - Unattended replay also requires the capability itself to be approved.
- **Redaction.**
  - Credentials resolve from `secret://` into memory only. The model sees
    them only as `${credentials...}` placeholders.
  - Every resolved value and every sensitive input is replaced with `***` in
    what the model sees and in every log, tree, result and trace. A field
    labelled like a secret is masked whatever it holds.
  - Screenshot masks are painted in when the picture is taken.
  - SSN and account-number shapes are scrubbed everywhere as a safety net.
  - A test searches every file of a live run for the password, and
    `make evidence` fails if its redaction scan finds anything.

**Limits.**
- Discovery necessarily sends screen content to a model. Synthetic data and
  masks mitigate this here; a deployment needs a model endpoint covered by
  the institution's data agreement.
- The trees under `observations/` still hold on-screen values such as names
  and balances. They are the redaction boundary and need the retention rules
  of the data they show.
- Shape scrubbing is context-free. A capability that must *return* an account
  number needs a policy change first.
- The person in a handoff is outside the allowlist, by design.
- CDP is full control of the session. It is bound to localhost here; remotely
  it needs authentication and a mediated viewer.

## 7. Cuts

| Cut | Why | Next |
|---|---|---|
| Desktop surface | One surface done properly. The seam exists | A UIA `Surface` |
| Tenant overlays | Designed above, not built: there is one tenant | Overlay loader, per-tenant rung roll-up, drift alerts |
| Vision locator rung, LLM fallback in replay | Both need a model at replay time, which breaks determinism | A bounded, policy-checked fallback that *proposes* a new ladder for review and never clicks |
| Real operator console | Out of scope in the brief. Lease, CDP, capture and resume search are real; the page is minimal | Co-browsing view, request assignment, operator auth |
| Remote CDP | Localhost only | Authenticated proxy, or a VNC/WebRTC viewer |
| Idempotency and request stores | Files | A table keyed by tenant and key; the request queue as the first service |
| Stretch goals | One built: `cua catalog` lists approved capabilities as tool definitions and invokes them by name with typed arguments, including `escalated` → `cua resume` | A `--times N` stability report |

Two choices differ from where I started. A fault that a person could fix
returns `failure` unless the invocation asks for a handoff, because an
unattended caller should not be surprised by a human in the loop. The
committed capabilities are approved by me: approval is a person's decision,
and the gate exists to make that decision explicit and tie it to one exact
version.
