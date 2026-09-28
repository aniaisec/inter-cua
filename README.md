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

On Windows, `pip install -e ".[dev,windows]"` also installs `comtypes`, which
the desktop target (DeskCalc, through Windows UI Automation) needs. The `dev`
extra includes `vision` (Pillow and numpy), which the vision fallback
(`cua replay --vision`) needs; a plain install replays without it.

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
| Explain a run; capability health | `cua metrics run evidence/replay-success`, `cua metrics report` | nothing: reads run directories |
| Capability versions, lifecycle and health | `cua registry list`, `cua registry show <name>` | nothing: reads files |
| Drift on record, and its rates | `cua drift scan`, `cua drift report` | nothing: reads run directories |
| A candidate repair for a run that drifted | `cua drift propose <run>`, then `cua drift evaluate <name>` | nothing to propose; Chromium to evaluate (it starts its own mock app) |
| A workflow's plan and its checks | `cua workflow check open_member_subaccount` | nothing: reads files |
| Security benchmark: hostile screens, tampered artifacts, replayed consent | `cua security run` (`--offline`: no browser) | Chromium for the live scenarios (it starts its own mock app) |
| Run a workflow of approved capabilities | `cua workflow run open_member_subaccount --input ... --idempotency-key k --approval open=<token>` | mock app |
| Replay with the vision fallback | `cua record evidence/discovery-member-savings-balance --out <dir>/member_savings_balance.json`, `cua describe` and `cua approve` it, then `cua replay <dir>/member_savings_balance.json --input member_id=10003 --inject hidden_control --vision` | mock app, the `vision` extra |
| Discover and replay a desktop application | `cua discover --tenant desk --policy policies/deskcalc.yaml --llm scripted --script scripts/discovery/deskcalc_compute.yaml ...`, `cua replay capabilities/deskcalc_compute.json --tenant desk --policy policies/deskcalc.yaml --input first=12.5 --input second=4 --input operation=Divide` | Windows and the `windows` extra; each run starts its own DeskCalc |

The browser tests start their own mock app on a free port, and the desktop
tests (`-m desktop`, Windows only, skipped elsewhere) their own DeskCalc. No
test calls a model: the discovery tests use the scripted client and recorded
fixtures.

## CLI

| Command | What it does | Exit |
|---|---|---|
| `cua discover --goal ... --param name:type=value --output name:type` | LLM observe → decide → act loop until the goal is met, or a step limit, time limit, dead end or `stuck`; records a draft capability | 0 done, 3 escalated, 1 stopped |
| `cua discover ... --llm scripted --script <file>` | same loop, no key | as above |
| `cua record <run_dir>` | turn a finished discovery run into a draft capability | |
| `cua describe <capability>` | the capability in plain words: inputs, outputs, side effects, steps and how each control is found, possible outcomes | |
| `cua approve <capability> --by <name>` | draft → approved, only as last described; registers the version | |
| `cua replay <capability> --input k=v` | deterministic replay; prints a JSON `ReplayResult` | 0 success, 2 business outcome, 1 failure, 3 escalated |
| `cua replay ... --inject <mode>` | arm a mock-app failure mode for the run | |
| `cua replay ... --approval-token <t> --idempotency-key <k> --budget timeout_s=120,max_recoveries=3,allow_escalation=false` | invocation-level consent, retry safety and limits | |
| `cua replay ... --handoff [--headed]` | route what a person could fix to the operator console instead of failing | |
| `cua approval-token <capability> --input k=v --by <name>` | sign consent for one invocation's risky step | |
| `cua resume <resume_token> [--resume-at <step>] [--approval-token <t>]` | carry on an `escalated` run from another process; a token answers `NEEDS_APPROVAL` without the console | as replay |
| `cua catalog [--json]` | the approved capabilities, typed; `--json` prints them as tool definitions for a model | |
| `cua catalog invoke <name> [--version N] --args '<json>' \| --input k=v \| --request <file>` | invoke by name with typed arguments (the highest approved version unless one is named); takes the replay invocation flags | as replay |
| `cua registry list \| versions <name> \| show <name> [--version N] \| health <name>` | every version of every capability, its lifecycle status and history, and its health from the replays on record | |
| `cua registry deprecate \| revoke \| reinstate <name> --version N --by <name> [--reason ...]` | move a version through its lifecycle; recorded in `capabilities/registry/lifecycle.jsonl` | |
| `cua registry sync` | register approved capabilities that are not registered yet | |
| `cua drift scan [--capability C] [--kind K]` \| `cua drift report [--exclude-injected]` | drift events classified from run directories (`CONTROL_RENAMED`, `CONTROL_MISSING`, `CONTROL_AMBIGUOUS`, `LAYOUT_CHANGED`, `FRAME_CHANGED`, `CHECKPOINT_CHANGED`, `OUTPUT_CHANGED`, `NAVIGATION_CHANGED`), and drift events per invocation by capability version and tenant, and per lookup by locator rung | |
| `cua drift propose <run_id \| dir>` | a candidate repair for the drift that stopped a run, under `capabilities/candidates/<name>/v<N>/`, as a draft; changes nothing else | 1 if the drift cannot be repaired from its evidence |
| `cua drift evaluate <name> [--version N] [--task T] [--repetitions R]` | replay the candidate beside the version it repairs on that capability's benchmark tasks, and record the gates in the candidate | 0 passed, 1 failed |
| `cua drift candidates [<name>]` \| `cua drift show <name> [--version N]` \| `cua drift reject <name> --version N --by <name> --reason ...` | candidate repairs, where each stands, and turning one down | |
| `cua workflow list \| check <workflow>` | the workflows in `workflows/`, each step's resolved version, the bindings and their types, and every problem found before running | check: 0 ok, 1 problems |
| `cua workflow run <workflow> --input k=v --idempotency-key <k> [--approval <step>=<token>] [--handoff]` | run approved capabilities in order through replay, wiring inputs and outputs; prints a JSON `WorkflowResult` | as replay |
| `cua workflow approval-token <workflow> --step <id> --input k=v --by <name>` | sign consent for one committing step, for the inputs that step will get | |
| `cua security list \| run [--offline] [--scenario ID]` | stage each attack in `bench/security/scenarios.yaml` with a scripted model that obeys every injected instruction; write `bench/security/reports/summary.{md,json}` | 0 every attack blocked, 1 otherwise |
| `cua operator` | the operator console on :8100 | |
| `cua serve [--port 8200] [--access api/access.yaml] [--workers 2]` | the capability runtime over HTTP: invoke approved capabilities and get the same `ReplayResult`; approve, resume and abort escalated runs; read their events | |
| `cua mcp [--client local-agent] [--tenant local] [--root DIR] [--handoff consent\|all\|none]` | approved capabilities as MCP tools over stdio, for an MCP-compatible agent; calls go through the same run service as `cua serve` | |
| `cua benchmark list \| run \| report` | run the benchmark suite (`bench/tasks/`) through the repeated-LLM baseline, discovery and replay; aggregate `bench/reports/runs.jsonl` into `summary.md` ([bench/README.md](bench/README.md)) | |
| `cua metrics run <run_id \| dir> [--json \| --events]` | one run explained: outcome and why, where the time went, model calls, tokens and estimated cost, locators, recoveries, human intervention; `--events` prints its canonical events | |
| `cua metrics capability <name> [--version N]` \| `cua metrics report [--out DIR]` | health of each approved capability from its replays (success, failure, escalation, human, locator failure, drift, unknown side effect, latency, last success and failure), and model spend | |
| `cua surfaces` | this build's surface adapters and their features, and for each registered capability the features it needs and whether it runs here | |
| `cua schema` | regenerate `capabilities/schema/capability-1.3.json` | |
| `cua mockapp` | the target app on :8000 | |

## Calling a capability from an agent

`cua catalog` is the surface an agent sees. It is a view of the capability
registry: one tool per capability, the highest approved version, because only
an approved version runs unattended. Deprecated and revoked versions are never
offered.
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

## Calling a capability over HTTP

`cua serve` puts the same runtime behind HTTP, for an agent or a service that
should not start a CLI process per call. It binds to `127.0.0.1:8200`; the
OpenAPI page is at `/docs`.

```bash
cua mockapp                  # in one terminal
cua serve                    # in another; creates .cua/api-keys/local-agent.key on first start
```

[`api/access.yaml`](api/access.yaml) says who may call it. Every request but
`GET /health` carries three things:

- `Authorization: Bearer <key>`: which client is calling. Keys are bound like
  tenant secrets, to a file or an environment variable, never written in the
  access file.
- `X-Cua-Tenant: <id>`: the tenant the call is for. It is never inferred.
- `X-Request-Id: <id>`, required on every POST and echoed on every answer.
  It ties the caller, the server's request log (`<runs>/.api/requests.jsonl`)
  and the run record together.

A client names its tenants, the capabilities it may see and run, and its
scopes: `read`, `invoke`, `approve` (carry a signed approval token into a
run) and `operate` (resume and abort escalated runs).

| Endpoint | What it does |
|---|---|
| `GET /health` | up or not; no key needed |
| `GET /capabilities` \| `/capabilities/{name}` \| `/capabilities/{name}/versions` | what `cua catalog` shows, for the tenant's application family and the client's capabilities: the tool definition, the contract, the lifecycle. No steps, locators or credential references |
| `POST /runs` | start a run: `{"capability", "version"?, "inputs", "approval"?, "budget"?, "handoff"?}`, typed like `catalog invoke --args` |
| `GET /runs/{id}` | the run, and its `ReplayResult` once there is one |
| `GET /runs/{id}/events` | its canonical events (`cua metrics run --events`) |
| `POST /runs/{id}/approve` | `{"token", "approved_by"?}`: consent for the commit an escalated run stopped at |
| `POST /runs/{id}/resume` | hand an escalated run back to the automation, as `cua resume` does |
| `POST /runs/{id}/abort` | end an escalated run as `ESCALATION_ABORTED`, as the console's Abort does |

A run gets its id at once and executes on a worker. The POST answers `202`
while the run executes, or waits for it with `?wait=<seconds>` (up to 300).
The run's `result` is the `ReplayResult` that `cua replay` prints: the same
fields, in the same order. It is `null` until the run stands still in one of
these states:

- `escalated`: waiting on a person or on consent;
- `finished`: success, business outcome or failure;
- `error`: the request could not be made at all, as `cua replay` exits 64;
- `lost`: the server stopped mid-run and the run directory holds no result.

```bash
KEY=$(cat .cua/api-keys/local-agent.key)
curl -s -X POST "http://127.0.0.1:8200/runs?wait=60" \
  -H "Authorization: Bearer $KEY" -H "X-Cua-Tenant: local" -H "X-Request-Id: agent-req-1" \
  -H "Content-Type: application/json" \
  -d '{"capability": "member_savings_balance", "inputs": {"member_id": "10003"}}'
```

A capability that is not read-only needs an `Idempotency-Key` header, and is
refused with `428` without one. The same key and request return the run it
first started (`Idempotent-Replayed: true`), including while that run is still
executing. The same key with other inputs is `409`. Keys are scoped to the
client and tenant: the runner caches results under `api:<tenant>:<client>:<key>`,
and the caller gets its own key back. A commit follows the CLI's path over
HTTP:

1. `POST /runs` with `"handoff": {}` comes back `escalated` (`NEEDS_APPROVAL`)
   with the browser left up.
2. `POST /runs/{id}/approve` with a token from `cua approval-token` carries it
   on. A token for other inputs is `403`, and the run keeps waiting.

Approving over HTTP takes a signed token, never a name: there is no person at
a console to vouch for who is consenting.

## Calling a capability over MCP

`cua mcp` serves the approved capabilities to any agent that speaks the Model
Context Protocol, over stdio. The agent sees business operations, not a
browser:

```text
member_savings_balance(member_id)
open_subaccount(member_id, initial_deposit, idempotency_key, approval_token?)
cua_run_status(run_id)  cua_approve_run(run_id, approval_token)  cua_resume_run(run_id)  cua_abort_run(run_id)
```

There is no `click`, `type` or `press`. Each capability tool's description
is its contract:

- its purpose and outputs;
- its side effect and whether it is idempotent;
- its business outcomes;
- when it will ask for consent.

Its input schema is the capability's typed inputs. A tool that changes
something requires an `idempotency_key`. A tool that may ask for consent takes
an optional `approval_token`, if the client has the `approve` scope. The
annotations say which tools are read-only, destructive or idempotent.

A call goes where a `POST /runs` goes: the registry resolves the version, the
arguments are typed, and the access file's client, tenant and scopes apply.
Replay then runs with no model, and every call is recorded in
`<runs>/.api/requests.jsonl`. The answer is the `ReplayResult`, reduced to what
an agent acts on: `kind`, the code or reason, `outputs`, `side_effect` and
`message`. How the GUI was driven stays in the run directory: locator rungs,
screenshots, the observed screen text. An `escalated` answer adds
`resume_token` and `required_action`, which says what has to happen and which
run tool carries the run on.

To use it from Claude Code (any MCP client takes the same command):

```bash
claude mcp add inter-cua -- "$PWD/.venv/Scripts/cua.exe" mcp --root "$PWD"
```

Over stdio the client is whoever started the process, so no key is asked
for. `--client` names the entry in `api/access.yaml` whose tenants,
capabilities and scopes it acts under. By default only capabilities that may
ask for consent run with a handoff (`--handoff consent`). A commit without a
token therefore comes back `escalated` with the session waiting, while a
lookup that gets stuck is a plain `failure`.

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
| – | Registry | | versions coexist after a re-record; only the allowed lifecycle moves; a revoked version starts nothing, a cached retry still answers; the catalog agrees with the registry | `unit/test_registry.py` |
| – | Drift and candidate repair | `renamed_button` | the failure is classified `CONTROL_RENAMED`; a candidate v4 is proposed and evaluated (v3 fails the drift task, v4 answers it, the clean task still passes); nothing in `capabilities/` outside `candidates/` changes and v3 stays the default; approval refused until the evaluation passed on that exact content | `integration/test_drift.py`, `unit/test_drift.py` |
| – | Workflow composition | | lookup then open with consent for the open only: one commit, typed outputs from both steps; the same key again answered without a browser; an unknown member stops at the lookup with no commit; wiring mistakes, missing or misdirected consent, a revoked step and a missing key refused before any step; a lost answer, an escalated step and a killed commit never repeat a commit | `integration/test_workflow.py`, `unit/test_workflow.py` |
| – | Desktop target | `renamed_button`, `ambiguous`, `disabled`, `modal` | DeskCalc under Windows UI Automation, through the unchanged runner: `success` with every step on `role_name`; divide by zero is `business_outcome DIVIDE_BY_ZERO`; the four faults stop with the web's codes (`LOCATOR_UNRESOLVED`, `ACTION_FAILED`, the message box answered and reported); Record refused without consent, one token one ledger entry, the same key answered from the cache; a run asking for screenshots refused before DeskCalc starts | `desktop/test_desktop_replay.py`, `desktop/test_windows_surface.py`, `unit/test_windows_surface.py` |
| – | Vision fallback | `hidden_control`, `hidden_duplicate`, `renamed_button` | with `--vision`, a Search button hidden from the tree is found by its recorded picture and the run succeeds (rung `vision`, checkpoint still checked); two identical hidden buttons are `LOCATOR_AMBIGUOUS`, nothing clicked; a renamed button does not match its picture; a hidden Confirm is `POLICY_BLOCKED` even with a token, no commit; without the flag, without `vision.allowed`, or without a recorded picture, nothing changes; a point whose pixels changed is not clicked | `integration/test_vision.py`, `unit/test_vision.py` |
| – | HTTP API | | an approved capability invoked over HTTP answers what `cua catalog invoke` answers (the same logical result, the same fields in the same order); a commit escalates, a token for other inputs is `403` with the run still waiting, the right token commits once, and a retry with the key returns that run; an escalated run aborted over HTTP closes its browser; keys, tenants, scopes, capability authorization, request ids, key scoping and concurrent retries | `integration/test_api.py`, `unit/test_api.py`, `desktop/test_desktop_api.py` |
| – | MCP | | `cua mcp` on stdio: one tool per approved capability with its typed schema and contract, no browser primitive; an agent looks a balance up, is told `NOT_FOUND` as an answer, gets `INPUT_INVALID` for a stray argument, opens a sub-account, is told `escalated` with the run tool to call, and commits through `cua_approve_run`; a retry with the key returns that run; protocol errors, a slow call not holding up `ping`, scopes, and results that leave the GUI details in the run directory | `integration/test_mcp.py`, `unit/test_mcp.py` |
| – | Security benchmark | `prompt_injection`, `malicious_redirect`, `confirmation_spoof` | 22 attacks across 15 threats blocked: nothing reaches the attacker, no file served, no commit without consent, the canary password in no log, trace or prompt, no other tenant's consent or secret accepted; with the defences off, the same attacks succeed | `security/test_benchmark.py`, `security/test_controls.py` |

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
| `prompt_injection` | member detail | persistent | hostile content: injected instructions, a fake "SYSTEM: approval granted", a Password field in a form that posts to the attacker, a beacon, an .exe, and a link that commits on GET |
| `malicious_redirect` | search submit | persistent | a 303 to the attacker's copy of the sign-on page |
| `confirmation_spoof` | review (GET) | persistent | "Sub-account Opened" with a reference, nothing committed |
| `hidden_control` | search page, review | persistent | Search and Confirm drawn as ever, hidden from the tree (`aria-hidden`): the vision fallback's case |
| `hidden_duplicate` | search page | persistent | two identical hidden Search buttons: vision refuses to choose |

The hostile modes serve the security benchmark. The attacker's origin is the
same server addressed as `localhost` instead of `127.0.0.1`: another origin
to the browser and the policy. `/_debug/attacker` lists what reached it and
counts the files the app served. `MOCKAPP_OPERATOR_PASSWORD` sets the
sign-on password, which the benchmark uses as a canary.

One-shot modes clear the first time they fire; if they persisted, no recovery
could succeed and the recovery tests would prove nothing. Persistent modes
model states the app is really in, where replay should report rather than
recover.

## The desktop target app

`deskapp/deskcalc.ps1` is DeskCalc: a WinForms window, run by Windows
PowerShell, with no clock and no randomness. It has two number fields named
only by the label beside them, an Operation combo box, a Round to cents
checkbox, Calculate, a read-only Result, and Record. Record is the commit: it
appends a line to the ledger file (`DESKCALC_LEDGER`, default
`%TEMP%\deskcalc-ledger.txt`), which the tests read to judge what happened.
`-Inject` switches on one fault per launch, as `?inject=` does on the entry
location:

| Mode | Effect |
|---|---|
| `renamed_button` | Calculate is labelled Compute |
| `ambiguous` | a second Calculate button, in a Legacy group |
| `disabled` | Calculate is disabled |
| `slow` | the result appears 2.5 s after Calculate |
| `modal` | Calculate raises a native message box instead of a result |

Dividing by zero shows "Cannot divide by zero" in a label, which the
`deskcalc` app family (`capabilities/families/deskcalc.yaml`) turns into the
business outcome `DIVIDE_BY_ZERO`.

## How it works

The reasoning behind each choice is in [REPORT.md](REPORT.md). This is the
map of the code.

```
mockapp/            the automation target
deskapp/            the desktop target: DeskCalc, a WinForms window (PowerShell)
src/cua/surface/    Surface protocol, a11y perception, locator ladder, conditions, Playwright adapter
src/cua/surface/windows/  the Windows UI Automation adapter
src/cua/surface/vision/   the vision fallback: detector, candidate, validator
src/cua/agent/      discovery loop, prompts, tools, stopping conditions, LLM clients (Claude, Gemini, scripted)
src/cua/artifact/   capability schema, recorder, store (versioning, content seal), describe
src/cua/replay/     engine, waits, detectors, recoverers, resume-state search, result contract, runner
src/cua/policy/     allowlist and risk, approval gate, approval tokens, redaction
src/cua/secrets/    secret:// resolver (environment or file, memory only)
src/cua/escalation/ control lease, state machine, intervention requests, operator console, human-action capture
src/cua/evidence/   run log, trace, `make evidence` builder
src/cua/observability/ canonical events read from run directories, run metrics, capability health, cost
src/cua/benchmark/  repeated-LLM baseline vs discover-then-replay (`bench/`)
policies/           allowlist, risk rules, masks and scrub patterns
tenants/            per-deployment binding: base_url, secret refs, overlay
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
    inputs: {member_id: ${member_id}}
  - id: open
    capability: open_subaccount
    inputs: {member_id: ${member_id}, initial_deposit: ${initial_deposit}}
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
the model refusing an injected instruction ([docs/THREAT_MODEL.md](docs/THREAT_MODEL.md),
[SECURITY.md](SECURITY.md)). On top of the policy above:

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
- **Screen content is labelled** as the application's in the prompt. This is
  defense in depth; no test relies on it.

`cua security run` stages 22 attacks across 15 threats, 11 live against a
fresh mock app and 11 offline. The "model" is a script that follows every
injected instruction. Each attack is judged by its effects: requests that
reached the attacker's origin, files served, commits, and where a canary
password turned up. The latest report is
[bench/security/reports/summary.md](bench/security/reports/summary.md). The
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

## License

MIT.
