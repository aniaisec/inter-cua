# Development and test coverage

## Development

GitHub Actions runs quality and browser gates and uploads fresh benchmark/security
reports; see [CI gates and artifacts](CI.md) for coverage and reproduction.
Install `.[dev]` for both provider SDKs, vision and test/build tools. To reproduce
CI and benchmarks on Python 3.11, use
`python -m pip install -c constraints/dev-py311.txt -e ".[dev]"` in a fresh
environment. Other supported Python versions resolve the declared ranges;
the snapshot is specific to Python 3.11. See [packaging](packaging.md).

The local gate is `make test`; CI separately adds strict browser verification. On Windows without Make, use the equivalent
commands in the activated environment:

```bash
python -m ruff check .
python -m ruff format --check .
python -m mypy
python -m pytest
```

For a quick check without GUI automation, use
`python -m pytest -m "not browser and not desktop"`. Browser tests start their
own mock app; desktop tests require Windows and the `windows` extra.
For a small benchmark harness check without a model key:

```bash
cua benchmark run --suite core --task lookup-success --repetitions 1
cua benchmark report --latest
```

The default scripted model checks the harness. A live comparison requires
`--llm gemini` or `--llm anthropic`, credentials, and paid model calls. See
[benchmark setup and reproduction](../bench/README.md) for repetitions, strategies,
raw metrics, statistical reports and configurable pricing.

## Service requirements

| What | Command | Needs |
|---|---|---|
| Full gate: lint, format, `mypy --strict`, every test | `make test` | Chromium |
| Tests that need no browser | `python -m pytest -m "not browser and not desktop"` | nothing |
| Discovery with no model | `cua discover --llm scripted --script scripts/discovery/<name>.yaml ...` | mock app |
| Replay the committed, approved capabilities | `cua replay capabilities/<name>.json --input ...` | mock app |
| Regenerate every replay under `evidence/` | `make evidence` | port 8000 free |
| Benchmark: repeated LLM vs discover-then-replay | `cua benchmark run` then `cua benchmark report --latest`; `--suite all` for the six category suites (browser, recovery, drift, side effects, security, composition) | Chromium and isolated mock services even when scripted; a key and paid calls with a live `--llm` |
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
| – | Tenant isolation | | two tenants on one product: a capability the tenant does not run, another tenant's credential reference or a system secret is `POLICY_BLOCKED` before anything is read; another tenant's approval token is refused before a browser; one idempotency key on two tenants is two requests (replay's cache and workflow journals), and an older record is its run's tenant's only; one tenant's console shows and decides none of another's requests; no two tenants share a secret, and the API refuses to serve two that do | `security/test_tenant_isolation.py` |
| – | Benchmark categories | `changed_label`, `moved_field`, `changed_frame`, `moved_output`, `server_error_once` | six category suites load with every scenario each covers; a workflow task, a task asked of the model alone (upload, download), and five forms of consent, each wrong token refused for its own reason; any download, upload or attacker contact scores a run wrong; a per-category report; the new drift and error modes, the directory's filter and pages, and uploads that count only with content | `unit/test_benchmark_suites.py`, `integration/test_mockapp_smoke.py` |
| – | Security benchmark | `prompt_injection`, `malicious_redirect`, `confirmation_spoof` | 22 attacks across 15 threats blocked: nothing reaches the attacker, no file served, no commit without consent, the canary password in no log, trace or prompt, no other tenant's consent or secret accepted; with the defences off, the same attacks succeed | `security/test_benchmark.py`, `security/test_controls.py` |

## Strict browser verification

Use `python -m pytest --require-browser -m "browser and not desktop"` for release
verification. Every Chromium launch error fails; strict mode also fails when no
browser tests execute. Optional mode skips only the explicit missing-executable
condition. Tests use scripted models, not paid providers. Live desktop tests need
interactive Windows and the `windows` extra; skips are not desktop verification.

[Documentation index](index.md) · [Project README](../README.md)
