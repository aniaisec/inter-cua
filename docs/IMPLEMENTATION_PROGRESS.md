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
