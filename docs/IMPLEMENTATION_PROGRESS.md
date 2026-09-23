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
