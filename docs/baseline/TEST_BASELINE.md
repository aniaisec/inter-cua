# Test and behaviour baseline

The state of inter-cua before the benchmark, observability and registry work
begins. Later changes are measured against these numbers. The git tag
`inter-cua-baseline` marks the commit they were taken on. It is never moved.

Measured 2026-09-23 on `03133bf` (tag `v1.0.1`), working tree clean.

## Environment

| Item | Value |
|---|---|
| OS | Windows 11 Home 10.0.26200 (build 26200.9457), x86_64 |
| Python | 3.14.5 (project venv) |
| Playwright | 1.63.0 |
| Browser | Chromium 153.0.8010.12 (Playwright build, headless) |
| pytest / ruff / mypy | 9.1.1 / 0.16.8 / 2.3.1 |
| Discovery model | `CUA_GEMINI_MODEL=gemini-flash-latest`, served as `gemini-3.8-flash` |

## Gate (`make test`)

`make` is not installed on this machine, so the `test` target's commands were
run one after another: `ruff check .`, `ruff format --check .`, `mypy`,
`pytest`.

| Step | Result |
|---|---|
| `ruff check .` | All checks passed |
| `ruff format --check .` | 94 files already formatted |
| `mypy` (strict, `src/`) | no issues in 59 source files |
| `pytest` | **443 passed**, 0 failed, 0 skipped, 0 errors |
| of which `-m "not browser"` | 364 |
| of which `-m browser` | 79 |

Runtime:

| Run | Static checks | pytest | Total |
|---|---:|---:|---:|
| 1 (plain `pytest`) | < 1 s (warm mypy cache) | 400.7 s | 401.5 s |
| 2 (with `--junitxml`) | n/a | 340.0 s | n/a |

The spread between the two runs is ordinary machine noise. Browser tests
account for nearly all of the time. The slowest are handoff and resume
(17–20 s each) and the slow-confirm replay (16 s).

Known failures: none.

## Benchmark scenarios available today

No benchmark harness exists yet: there is no repetition runner, no stored
per-run metrics, and no repeated-LLM baseline strategy. What exists and a
harness can build on:

- **13 mock-app inject modes** (`mockapp/injects.py`): `not_found`,
  `validation_error`, `permission_denied`, `interstitial_dialog`,
  `interstitial_persistent`, `slow_load`, `session_expired`, `server_error`,
  `renamed_button`, `ambiguous_button`, `slow_confirm`, `modal_dialog`,
  `native_confirm`.
- **Two approved capabilities** in `capabilities/`: `member_savings_balance`
  (v3, read-only) and `open_subaccount` (irreversible commit, needs approval).
- **Two discovery scripts** in `scripts/discovery/` for key-free discovery.
- **Per-run records** already written by every run: `result.json`,
  `log.jsonl`, and for discovery `model_calls.jsonl` with token usage per call.

## Demo flow (README "Demo path", steps 1–3)

The mock app was served on `127.0.0.1:8000`, and every command ran from
PowerShell. Outputs went to `demo/baseline-*` and `evidence/runs/`, both
gitignored.

### Discovery

| | Live model | Scripted (`--llm scripted`) |
|---|---:|---:|
| Result | `done`, exit 0 | `done`, exit 0 |
| Steps | 6 | 6 |
| Discovery duration (loop) | 22.4 s | 1.8 s |
| Wall clock (CLI, incl. startup) | 24.2 s | 2.8 s |
| Model calls | 6 | 0 (recorded tool calls) |
| Input tokens | 36,565 | 0 |
| Output tokens | 240 | 0 |
| Thinking tokens | 389 | 0 |
| Cache-read tokens | 0 | 0 |
| Outputs | `savings_balance=1411.21`, `member_name=Test Member 03` | same |
| Evidence | `log.jsonl`, `model_calls.jsonl`, 15 masked screenshots + observations, `run.json`, `result.json`, `script.yaml` (184 KB) | same layout |

Input tokens grow each turn (2.4k → 8k+) because every turn resends the
growing observation history. That growth is the main cost driver per model
call.

`cua describe`, then `cua approve --by reviewer`, turned the live draft into
approved version 1. Replaying the unapproved scripted draft was refused with
`POLICY_BLOCKED` before a browser started.

### Replay (live-discovered capability, no model)

| Case | Exit | Kind / code | Loop duration | Wall | Actions | Model calls |
|---|---:|---|---:|---:|---:|---:|
| `member_id=10003` | 0 | success, 1411.21 | 4.97 s | 6.0 s | 5 | 0 |
| `member_id=99999` | 2 | business_outcome `NOT_FOUND` | 3.88 s | 5.0 s | 5 | 0 |
| `--inject session_expired` | 0 | success after 1 recovery | 9.50 s | 10.6 s | 10 | 0 |
| `--inject server_error` | 1 | failure `APP_ERROR`, `trace.zip` | 4.04 s | 5.4 s | 5 | 0 |
| `member_id=ten` | 1 | failure `INPUT_INVALID`, no browser | 0 s | 0.4 s | 0 | 0 |
| draft capability | 1 | failure `POLICY_BLOCKED`, no browser | 0 s | 0.4 s | 0 | 0 |
| committed `capabilities/…` v3 | 0 | success, 1411.21 | 5.21 s | 6.3 s | 5 | 0 |

Repeated success (the first success run plus 10 repeats, n = 11): 11/11
success, same balance every time, 5 actions and 0 model calls each, and the
same locator rungs every time.

| | Min | Median | Mean | Max |
|---|---:|---:|---:|---:|
| Replay loop (`duration_ms`) | 4.97 s | 5.29 s | 5.33 s | 5.82 s |
| Wall clock (CLI) | 6.00 s | 6.35 s | n/a | 6.85 s |

Replay evidence per success run: `result.json`, `log.jsonl`, 6 masked
screenshots plus observations (about 196 KB). A failure adds a Playwright
`trace.zip` (about 456 KB for `server_error`).

These samples are small and taken on one machine. They are a reference point,
not a benchmark result.

## Invariants confirmed at baseline

- Replay made no model calls and wrote no `model_calls.jsonl`. The existing
  test that model SDKs are absent from the replay import path passes.
- Draft capabilities are refused by replay.
- Typed input is validated before a browser starts.
- Failure results carry evidence (a trace on `APP_ERROR`).

## Reproduce

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest
```

For the demo, follow the README "Demo path" with `--capabilities-dir
demo/baseline-live`. Run it from PowerShell, not Git Bash: MSYS path
conversion rewrites `--entry /login` into a Windows path, and discovery then
correctly escalates as `STUCK` on a 404.
