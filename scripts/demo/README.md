# Reproducible demo (Phase 18)

With the package and Chromium installed, `cua demo --repetitions 3` runs from any
directory, including a non-editable wheel installation. The runner
starts its own mock app and operator console on free ports; it never stops an
existing service or replaces the repository's capabilities or curated evidence.

```powershell
# Full demonstration: 100 independent lookup replays (no model key needed)
.venv\Scripts\python.exe scripts/demo/run_demo.py --out demo/phase18

# Short verification: all seven stages, with three repeated lookups
.venv\Scripts\python.exe scripts/demo/run_demo.py --out demo/phase18-smoke --repetitions 3

# Rebuild the report from an existing completed session, without executing UI actions
.venv\Scripts\python.exe scripts/demo/generate_report.py demo/phase18
```

On other platforms, use `python` from the activated environment. `make demo`
uses the same runner, with `ARGS` forwarded. Without `--out`, each invocation
creates a new `demo/demo_<session-id>/`. An explicitly named directory must
not exist: the runner refuses to overwrite it. Use a different name to rerun.
The default 100 replay demonstration can take 10–20 minutes depending on load.
The `cua demo` command defaults to two replays; the compatibility script above
retains its 100-replay default. Starter files come from `cua.resources`, also
used by `cua init PATH --template demo`. No repository keys, approval receipts,
or checkout paths are copied; synthetic capability approval happens explicitly
inside each session.

## What runs

1. **Discovery:** the existing tool-call script operates the actual browser and
   records a draft. `draft.json` preserves that artifact. A live model is optional.
2. **Review and approval:** draft replay is refused, then `cua describe` and
   `cua approve --by demo-reviewer` approve that exact artifact. `approved.json`
   preserves it. The reviewer is scripted for this local demo.
3. **Repeated replay:** each invocation gets a new run directory and browser;
   all must return `1411.21` without side effects. No idempotency cache is used
   to inflate the repetition count. The runner and report check the run files
   for model calls; the existing architectural tests enforce model-free replay.
4. **Drift:** Search becomes Find. Replay escalates at `search.submit` and exits
   with a resume token, leaving its browser alive.
5. **Takeover and resume:** the existing `evidence-bot` takes control through
   the real operator console, clicks Find through CDP, and hands back. A separate
   `cua resume` process reads the balance from the same session at `cp.done`.
6. **Consent:** no token blocks the commit; signed consent commits once; reuse
   without an idempotency key is rejected. Mock-app counters must increase by
   exactly one across all three calls.
7. **Hostile page:** the real `SEC-03-external-link` security scenario stages an
   injected navigation instruction. A script obeys it and the runtime blocks
   the unsafe action. Its security report must contain one blocked attack and
   no unsafe effects.

Scripted discovery demonstrates the discovery/recording mechanism, not an
LLM's planning quality. `evidence-bot` demonstrates the human-handoff mechanism;
its time is not a measurement of a person's work. The default run makes no
paid model calls. This is a demonstration, not a comparative benchmark.

## Optional live discovery

Install the selected `gemini` or `anthropic` extra (both are in `dev`). Set the
provider's key in the environment or the repository's `.env`, then
explicitly select a provider. Only discovery uses it; the operator and hostile
page model remain scripted, and replay remains deterministic.

```powershell
.venv\Scripts\python.exe scripts/demo/run_demo.py --out demo/phase18-live --llm gemini
# Or: --llm anthropic; optional --model <provider-model-id>
```

Live discovery incurs model charges and can fail to discover a usable artifact.
Failure does not auto-approve a draft or create a passing report.

## Output and failure handling

- `manifest.json`: stage status and command transcript, with approval and resume
  tokens replaced by placeholders; stderr and signing keys are not persisted.
- `draft.json`, `approved.json`: the discovered capability before and after review.
- `summary.md`, `summary.json`: generated only after all seven stages pass.
  Report regeneration verifies outcomes against replay/handoff result files,
  snapshots and the security report; missing or contradictory evidence fails.
- `workspace/`: isolated capabilities, tenant binding, review receipts, run
  directories, masked observations/screenshots, logs, handoff actions and security
  evidence. The session's signing key exists only in process environments.

On failure, the manifest remains with status `failed` and the exception type.
The runner aborts its pending handoffs through the console and stops its own
servers. Output under `demo/` is gitignored. It includes private runtime plumbing;
review and redact selected evidence before sharing it. It does not regenerate
or publish the curated package under `evidence/`.

## Verification

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_demo.py
.venv\Scripts\python.exe -m pytest tests/integration/test_demo.py
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy
```

The integration test exercises every stage with two replays. A passing full
session reports 100 independent successful replays, zero replay model calls,
one risky commit, and one blocked hostile-page scenario.
