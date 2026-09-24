# inter-cua

A model works out how to do a task in a legacy back-office UI once. What it
did becomes a typed, versioned, reviewable **capability**. An AI agent then
invokes that capability by name with typed inputs, and a deterministic replay
engine with no model in the process carries it out. When the replay cannot
safely go on, a person takes over the same live browser session and hands it
back.

```
goal ─► cua discover (LLM) ─► capability.json (draft) ─► cua describe / approve
                                                              │
     caller ◄── ReplayResult (success | business_outcome | failure | escalated)
                    ▲
                    └── cua replay (no model) ──► cua operator (a person, same session) ──► cua resume
```

The target is `mockapp/`, a deliberately hostile stand-in for a legacy
credit-union core: framesets, nested tables, no ids, unlabeled inputs, and
thirteen failure modes you can switch on per request.

- **Design write-up:** [REPORT.md](REPORT.md)
- **Evidence** (two real model discovery runs, seven replays including
  failures and handoffs): [evidence/](evidence/README.md)

## Setup

Python 3.11 or newer, and Chromium through Playwright.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env
```

`.env` holds everything the system reads from the environment:

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` | `cua discover` with a real model | Only discovery calls a model. Set either one; with both, Claude is used unless `CUA_LLM` says otherwise |
| `CUA_MODEL` | Claude model id | default `claude-sonnet-5` |
| `CUA_GEMINI_MODEL` | Gemini model id | default `gemini-flash-latest` |
| `CUA_SECRET_MOCKCORE_OPERATOR` | signing on to the mock app | `operator:operator` as shipped. Capabilities name it only as `secret://{tenant.id}/mockcore/operator` |
| `CUA_HEADED` | tests | `1` shows the browser |

Nothing else needs a key: the tests, `cua replay`, `cua operator`, and
`cua discover --llm scripted` all run offline.

## Demo path

Start the mock app in one terminal and leave it running:

```bash
make mockapp                       # or: cua mockapp   -> http://127.0.0.1:8000 (sign on operator / operator)
```

Everything else runs in a second terminal with the venv activated.

**1. Discover.** A model drives the app from the goal alone and the run is
recorded as a draft capability. `--capabilities-dir demo` keeps it out of the
committed `capabilities/`.

```bash
cua discover --goal "Look up a member by id and return the current savings balance" \
  --name member_savings_balance --entry /login \
  --param member_id:string=10003 \
  --output savings_balance:decimal --output "member_name:string?" \
  --capabilities-dir demo
```

It prints the run directory (`evidence/runs/<run_id>/`: the model's reason for
every action in `log.jsonl`, response ids and token counts in
`model_calls.jsonl`, masked screenshots) and `demo/member_savings_balance.json`.

No key? Add `--llm scripted --script scripts/discovery/member_savings_balance.yaml`
to the same command. The same loop, policy checks and recorder run; the
"model" plays back a recorded tool-call sequence.

**2. Review and approve.** Replay refuses a draft. `describe` renders the
capability for a person who is not an engineer; `approve` refuses unless the
capability is exactly what `describe` last showed.

```bash
cua describe demo/member_savings_balance.json
cua approve demo/member_savings_balance.json --by reviewer
```

**3. Replay.** No model is involved from here on. Each call prints one JSON
`ReplayResult`, and the exit code says which of the four kinds it is.

```bash
cua replay demo/member_savings_balance.json --input member_id=10003                          # 0 success: savings_balance 1411.21
cua replay demo/member_savings_balance.json --input member_id=99999                          # 2 business_outcome: NOT_FOUND at search.submit
cua replay demo/member_savings_balance.json --input member_id=10003 --inject session_expired # 0 success, after a re-login recovery
cua replay demo/member_savings_balance.json --input member_id=10003 --inject server_error    # 1 failure: APP_ERROR, expected/observed, trace.zip
cua replay demo/member_savings_balance.json --input member_id=ten                            # 1 failure: INPUT_INVALID, no browser started
```

**4. Hand a stuck run to a person.** Start the operator console in a third
terminal, then make the Search button read "Find", which the capability has
never seen:

```bash
cua operator                       # http://127.0.0.1:8100
```

```bash
cua replay capabilities/member_savings_balance.json --input member_id=10003 \
  --inject renamed_button --handoff --headed
```

The replay pauses and opens an intervention request. On the console press
**Take control**, click **Find** in the browser window, then **Resume**. The
run checks the screen against its checkpoints, finds the member detail page
already showing the balance (`cp.done`), reads it, and returns `success`
with the handoff in the result (`handoffs[0]`) and your click in the run's
`human_actions.jsonl`.

**5. A step that commits something.** Opening a sub-account ends on an
irreversible Confirm. Without consent the run does not press it; with a
signed, single-use approval token it runs unattended:

```bash
cua replay capabilities/open_subaccount.json --input member_id=10003 --input initial_deposit=250.00 --handoff
```

(pauses at `review.submit` with `NEEDS_APPROVAL`; **Approve action** on the console commits once)

```bash
cua replay capabilities/open_subaccount.json --input member_id=10003 --input initial_deposit=250.00 \
  --approval-token "$(cua approval-token capabilities/open_subaccount.json --input member_id=10003 --input initial_deposit=250.00 --by reviewer)"
```

(commits unattended; `side_effect: committed` and the reference number)

In PowerShell, write `` ` `` instead of `\` at line ends.

## Run without live services

| What | Command | Needs |
|---|---|---|
| Full gate: lint, format, `mypy --strict`, every test | `make test` | Chromium |
| Tests that need no browser | `python -m pytest -m "not browser"` | nothing |
| Discovery with no model | `cua discover --llm scripted --script scripts/discovery/<name>.yaml ...` | mock app |
| Replay the committed, approved capabilities | `cua replay capabilities/<name>.json --input ...` | mock app |
| Regenerate every replay under `evidence/` | `make evidence` | port 8000 free |
| Benchmark: repeated LLM vs discover-then-replay | `cua benchmark run` then `cua benchmark report --latest` | nothing with the scripted model; a key and money with `--llm` |

The browser tests start their own mock app on a free port. No test calls a
model: the discovery tests use the scripted client and recorded fixtures.

## CLI

| Command | What it does | Exit |
|---|---|---|
| `cua discover --goal ... --param name:type=value --output name:type` | LLM observe → decide → act loop until the goal is met, or a step limit, time limit, dead end or `stuck`; records a draft capability | 0 done, 3 escalated, 1 stopped |
| `cua discover ... --llm scripted --script <file>` | same loop, no key | as above |
| `cua record <run_dir>` | turn a finished discovery run into a draft capability | |
| `cua describe <capability>` | the capability in plain words: inputs, outputs, side effects, steps and how each control is found, possible outcomes | |
| `cua approve <capability> --by <name>` | draft → approved, only as last described | |
| `cua replay <capability> --input k=v` | deterministic replay; prints a JSON `ReplayResult` | 0 success, 2 business outcome, 1 failure, 3 escalated |
| `cua replay ... --inject <mode>` | arm a mock-app failure mode for the run | |
| `cua replay ... --approval-token <t> --idempotency-key <k> --budget timeout_s=120,max_recoveries=3,allow_escalation=false` | invocation-level consent, retry safety and limits | |
| `cua replay ... --handoff [--headed]` | route what a person could fix to the operator console instead of failing | |
| `cua approval-token <capability> --input k=v --by <name>` | sign consent for one invocation's risky step | |
| `cua resume <resume_token> [--resume-at <step>] [--approval-token <t>]` | carry on an `escalated` run from another process; a token answers `NEEDS_APPROVAL` without the console | as replay |
| `cua catalog [--json]` | the approved capabilities, typed; `--json` prints them as tool definitions for a model | |
| `cua catalog invoke <name> --args '<json>' \| --input k=v \| --request <file>` | invoke by name with typed arguments; takes the replay invocation flags | as replay |
| `cua operator` | the operator console on :8100 | |
| `cua benchmark list \| run \| report` | run the benchmark suite (`bench/tasks/`) through the repeated-LLM baseline, discovery and replay; aggregate `bench/reports/runs.jsonl` into `summary.md` ([bench/README.md](bench/README.md)) | |
| `cua schema` | regenerate `capabilities/schema/capability-1.1.json` | |
| `cua mockapp` | the target app on :8000 | |

## Calling a capability from an agent

`cua catalog` is the surface an agent sees. It lists only approved
capabilities, because those are the only ones unattended replay will run.
`--json` prints each one as a tool definition (`name`, `description`,
`input_schema`), the shape a model's tool-calling API takes. The description
carries the contract: outputs, side effect, whether it is idempotent, and
which business outcomes and escalations to expect.

```bash
cua catalog
cua catalog --json
cua catalog invoke member_savings_balance --args '{"member_id": "10003"}'     # 0 success
cua catalog invoke member_savings_balance --args '{"member_id": 10003}'       # 1 INPUT_INVALID: member_id is a string
```

Arguments are typed the way a model types a tool call. A `decimal` may come as
a number or a string, but a `string` must be a JSON string: a member id sent
as a number would lose any leading zero. A wrong type fails before a browser
starts. `--request <file>` (or `-` for stdin) takes a whole invocation request
(`capability`, `inputs`, `idempotency_key`, `approval`, `budget`) as JSON.

An invocation that needs a person returns `escalated` and exits, leaving the
browser up. The caller gets consent from someone who may give it and carries
the run on:

```bash
cua catalog invoke open_subaccount --input member_id=10003 --input initial_deposit=250.00 --handoff --handoff-wait 0
```

(exit 3: `escalated`, `NEEDS_APPROVAL` at `review.submit`, `side_effect: none`, and a `resume_token`)

```bash
cua resume <resume_token> --approval-token "$(cua approval-token capabilities/open_subaccount.json --input member_id=10003 --input initial_deposit=250.00 --by reviewer)"
```

(exit 0: `success`, `side_effect: committed`, the reference number. The resumed
run found the review screen still holding, so it carried on at
`review.submit` with the fresh consent. A token for other inputs is refused and
the run keeps waiting.) The same request also shows up on `cua operator`, where
**Approve action** does the same thing.

## Test matrix

Every scenario below is a pytest case (`tests/integration` run against the
mock app in Chromium; `tests/unit` do not).

| # | Scenario | Inject | Expected | Test |
|---|---|---|---|---|
| 1 | Member 10003 balance | | `success`, seeded balance, no pixel rung | `integration/test_replay.py::test_row01_*` |
| 2 | Unknown member | `not_found` | `business_outcome NOT_FOUND` at `search.submit`, exit 2 | `test_row02_*` |
| 3 | Empty deposit | `validation_error` | `business_outcome VALIDATION_ERROR`, `payload.field` | `test_row03_*` |
| 4 | Restricted member 10007 | `permission_denied` | `business_outcome PERMISSION_DENIED` with the name that could be read | `test_row04_*` |
| 5 | System notice after sign on | `interstitial_dialog` | `success`, recovery `INTERSTITIAL` | `test_row05_*` |
| 6 | 4 s detail page | `slow_load` | `success`, recovery `SLOW_LOAD` | `test_row06_*` |
| 7 | Session expires | `session_expired` | `success`, re-login, resumed after `cp.logged_in`, password never logged | `test_row07_*` |
| 8 | HTTP 500 | `server_error` | `failure APP_ERROR` with screenshot and trace | `test_row08_*` |
| 9 | Search button renamed | `renamed_button` | `failure LOCATOR_UNRESOLVED` (escalation reason `STUCK`); with `--handoff`, an intervention with a masked screenshot | `test_row09_*` (both files) |
| 10 | Two Search buttons | `ambiguous_button` | `success`; an ambiguous rung falls through | `test_row10_*` |
| 11 | Confirm answers slowly | `slow_confirm` | `failure TIMEOUT`, `side_effect: unknown`, Confirm pressed once | `test_row11_*` |
| 12 | Detector scope | | nothing fires on a clean run; session expiry is deaf while signing on | `test_row12_*`, `unit/test_replay.py::test_session_expiry_is_deaf_*` |
| 13 | Notice that keeps coming back | `interstitial_persistent` | `failure RECOVERY_EXHAUSTED` | `test_row13_*` |
| 14 | Commit with no token | | not pressed; with `--handoff` the person approves and it commits once | `test_row14_*` (both files) |
| 15 | Commit with a valid token | | `success` unattended, `side_effect: committed` | `test_row15_*`, `test_one_token_is_one_commit` |
| 16 | The person finishes the flow | `renamed_button` | resume finds `cp.done`, nothing re-executed | `integration/test_handoff.py::test_row16_*` |
| 17 | `allow_escalation=false` | `server_error` | `failure ESCALATION_ABORTED`, nobody asked | `test_row17_*` |
| 18 | The person aborts | `server_error` | `failure ESCALATION_ABORTED`, `HUMAN_IN_CONTROL → ABORTED` | `test_row18_*` |
| 19 | Same idempotency key twice | | stored result, one browser session | `test_row19_*` |
| 20 | Malformed input | | `failure INPUT_INVALID`, no browser | `test_row20_*` |
| 21 | Agent follows an external or download link | | blocked, agent told why, run goes on | `integration/test_discovery.py::test_row21_*` |
| 22 | Agent at a dead end | | escalated `DEAD_END` after 3 unchanged screens, no more model calls | `unit/test_discovery_loop.py::test_a_screen_that_never_changes_*` |
| 23 | `done` without a declared output | | rejected, the agent is told why | `unit/test_discovery_loop.py::test_done_missing_a_declared_output_*` |
| 24 | Replay imports no model | | `anthropic`, `google.genai`, `cua.agent` absent | `unit/test_replay.py::test_replay_imports_no_model_client_and_no_agent` |
| 25 | Unsafe capability | | refused: unknown action, unresolved `${x}`, duplicate ids, retryable irreversible step | `unit/test_artifact_schema.py::test_an_unsafe_or_malformed_*` |
| 26 | Recorder golden | | goal-1 run → the golden capability | `unit/test_recorder.py::test_the_goal_one_run_records_*` |
| 27 | Redaction | | SSN and long-number shapes masked; the password in no file of a run | `unit/test_safety.py::test_the_scrubber_*`, `integration/test_discovery.py::test_the_live_password_*` |
| 28 | State machine and lease | | illegal transitions raise; no automation act while a person holds the lease; paused time not counted | `unit/test_escalation.py` |
| 29 | Resume-state search | | the newest holding checkpoint, never before a commit | `unit/test_replay.py::test_resume_search_*` |
| – | Catalog (stretch) | | invoke by name → `escalated NEEDS_APPROVAL` → `cua resume` with fresh consent commits once; wrong types fail `INPUT_INVALID` | `integration/test_catalog.py`, `unit/test_catalog.py` |

## The mock target app

`mockapp/` imitates a legacy core banking UI so the perception and locator
layers have something real to fight:

- the signed-in shell is a `<frameset>` (nav and main), so perception must walk
  every frame
- every layout is a nested `<table>`; no `id`, `data-*` or ARIA attributes
- no `<label for>`, so **text inputs have no accessible name**, which is why
  the `near_text` locator rung exists
- buttons are `<input type=submit>`, so the accessible name is the `value`
  attribute and can change under a recorded capability
- form fields carry cryptic `name` attributes (`F_MBRID`, `F_DEPAMT`) because
  HTML forms need them. They are not in the accessibility tree, and nothing
  under `src/cua` uses them.

All data is synthetic: members 10001–10010, named `Test Member NN`. Member
10007 is restricted.

Arm a failure mode with `?inject=<mode>` on any request, the `X-Inject`
header, or `cua replay --inject`. The mode is stored in the session so it
fires on the screen it belongs to. `?inject=none` disarms.

| Mode | Fires at | Persistence | Exercises |
|---|---|---|---|
| `not_found` | search submit | persistent | `business_outcome NOT_FOUND` |
| `validation_error` | sub-account submit | persistent | `business_outcome VALIDATION_ERROR` |
| `permission_denied` | member detail | persistent | `business_outcome PERMISSION_DENIED` |
| `interstitial_dialog` | after sign on | one-shot | recovery: dismiss the notice |
| `interstitial_persistent` | after sign on, and every return to the shell | persistent | recovery is capped: `RECOVERY_EXHAUSTED` |
| `slow_load` | member detail (4 s) | one-shot | recovery: look again, then retry |
| `session_expired` | member detail | one-shot | recovery: sign on again, resume from the last checkpoint |
| `server_error` | member detail (HTTP 500) | persistent | `failure APP_ERROR` |
| `renamed_button` | search page ("Find") | persistent | `LOCATOR_UNRESOLVED`, drift, handoff |
| `ambiguous_button` | search page (two "Search") | persistent | the ladder falls through rather than guess |
| `slow_confirm` | review → confirm (6 s) | one-shot | `failure TIMEOUT, side_effect: unknown` |
| `modal_dialog` | member detail (in-page overlay) | one-shot | a click under an overlay faults instead of going nowhere |
| `native_confirm` | review → confirm (`window.confirm`) | persistent | undeclared: dismissed and reported, nothing committed |

One-shot modes clear the first time they fire; if they persisted, no recovery
could succeed and the recovery tests would prove nothing. Persistent modes
model states the app is really in, where replay should report rather than
recover.

## How it works

The reasoning behind each choice is in [REPORT.md](REPORT.md). This is the
map of the code.

```
mockapp/            the automation target
src/cua/surface/    Surface protocol, a11y perception, locator ladder, conditions, Playwright adapter
src/cua/agent/      discovery loop, prompts, tools, stopping conditions, LLM clients (Claude, Gemini, scripted)
src/cua/artifact/   capability schema, recorder, store (versioning, content seal), describe
src/cua/replay/     engine, waits, detectors, recoverers, resume-state search, result contract, runner
src/cua/policy/     allowlist and risk, approval gate, approval tokens, redaction
src/cua/secrets/    secret:// resolver (environment or file, memory only)
src/cua/escalation/ control lease, state machine, intervention requests, operator console, human-action capture
src/cua/evidence/   run log, trace, `make evidence` builder
policies/           allowlist, risk rules, masks and scrub patterns
tenants/            per-deployment binding: base_url, secret refs, overlay
capabilities/       approved capabilities, the app-family template, the exported JSON Schema
evidence/           discovery, replay and escalation runs
tests/              unit | integration (`-m browser` needs Chromium)
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

No CSS or XPath selector appears under `src/cua`. The one exception is the
document-level `body` anchor `aria_snapshot` requires, which never names a
control.

### Capability

The pydantic model in `src/cua/artifact/schema.py` is the source of truth;
`capabilities/schema/capability-1.1.json` is exported from it. A capability
carries its contract (`inputs`, `outputs`, `contract` with side effects,
idempotency and declared business outcomes, and `credentials` as `secret://`
refs), its `steps` (a locator ladder, `risk`, `approval`, `retry`, and what
must hold afterwards), compound `checkpoints` that bind the inputs,
`outcome_detectors` and `recoverers` from the app-family template
(`capabilities/families/legacy-core.yaml`), and `provenance` naming the
discovery run. Any change bumps the version and resets it to draft, including
a hand edit, which the content seal written on every save catches.

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

## License

MIT.
