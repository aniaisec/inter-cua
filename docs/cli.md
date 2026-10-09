# CLI reference

`cua --version` prints the version shared by the installed package metadata and
`cua.__version__`. Live discovery requires its [provider extra](packaging.md).

Use `cua --help` and `cua <command> --help` for complete flags. The nearest ancestor `cua.toml` selects the project; global `cua --root DIR <command>` selects it explicitly. MCP also supports `cua mcp --root DIR`. Explicit CLI file paths remain relative to the invocation directory. See [path resolution](configuration.md).

| Command | What it does | Exit |
|---|---|---|
| `cua init PATH --template demo\|web\|windows [--dry-run]` | create an editable project from packaged resources; refuse existing files; generate a private per-project signing key | 0 created/planned, 64 invalid/conflict |
| `cua doctor [--tenant local] [--json] [--probe-browser] [--probe-app]` | local setup checks; opt in to Chromium launch or an unauthenticated application HEAD request | 0 ready for checked prerequisites, 1 failed checks, 64 malformed invocation/project |
| `cua demo [--out DIR] [--repetitions N]` | packaged seven-stage synthetic demo with no provider key; default two independent replays | 0 complete, 1 failed, 64 invalid project |
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
| `cua operator [--tenant local]` | the operator console on :8100, for one tenant's intervention requests | |
| `cua serve [--port 8200] [--access api/access.yaml] [--workers 2]` | the capability runtime over HTTP: invoke approved capabilities and get the same `ReplayResult`; approve, resume and abort escalated runs; read their events | |
| `cua mcp [--client local-agent] [--tenant local] [--root DIR] [--handoff consent\|all\|none]` | approved capabilities as MCP tools over stdio, for an MCP-compatible agent; calls go through the same run service as `cua serve` | |
| `cua benchmark list \| run \| report [--suite core\|all\|<category>]` | run a benchmark suite (`bench/tasks/`) through the repeated-LLM baseline, discovery and replay; aggregate `bench/reports/runs.jsonl` into `summary.md` ([bench/README.md](../bench/README.md)) | |
| `cua metrics run <run_id \| dir> [--json \| --events]` | one run explained: outcome and why, where the time went, model calls, tokens and estimated cost, locators, recoveries, human intervention; `--events` prints its canonical events | |
| `cua metrics capability <name> [--version N]` \| `cua metrics report [--out DIR]` | health of each approved capability from its replays (success, failure, escalation, human, locator failure, drift, unknown side effect, latency, last success and failure), model spend, and human intervention | |
| `cua metrics humans [--out DIR]` | every request to a person by how it ended (approval, recovery, manual completion, abort, expired, ...), with the time on the person (mean, median, p95; queued and in control), browser actions, and who decided | |
| `cua surfaces` | this build's surface adapters and their features, and for each registered capability the features it needs and whether it runs here | |
| `cua schema` | regenerate `capabilities/schema/capability-1.3.json` | |
| `cua mockapp` | the target app on :8000 | |

## Results and exit codes

| Replay / catalog / workflow exit | Meaning | Caller action |
|---|---|---|
| 0 | `success` | Read typed outputs |
| 1 | `failure` | Inspect code and side effect |
| 2 | `business_outcome` | Handle the declared answer |
| 3 | `escalated` | Obtain consent or arrange takeover; retain the resume token |
| 64 | Usage or configuration error | Correct the request |

Discovery has its own done/stopped/escalated results. Argparse errors such as an
unknown flag exit 2; that is not a replay business result. Decimal outputs are
strings to preserve precision. Non-finite decimal inputs and budgets are refused.
An unknown side effect requires reconciliation before another write. Keep the
original idempotency key when retrying a request whose answer was lost.

Doctor's argument errors use exit 64. With `--json`, stdout contains one JSON
object, including on invocation/configuration failures, and no status text is
interleaved. Report version 1 contains `report_version`, `ready`, `project`, and
`findings`. Each finding has a stable `code`, `severity` (`info`, `warning`,
`error`), `explanation`, `remedy`, and an optional identifying `subject` string.
Warnings do not fail readiness: drafts still need approval and optional provider
or vision dependencies matter only when using those features. Readiness does not
certify application behavior, authentication, or an interactive desktop session.
`--policy`, `--families-dir`, and `--capabilities-dir` follow explicit CLI path
rules. Probe details and remedies are in [troubleshooting](troubleshooting.md#doctor-findings).

[Documentation index](index.md) · [Project README](../README.md)
