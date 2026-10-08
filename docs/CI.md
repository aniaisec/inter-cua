# CI gates and artifacts

`.github/workflows/ci.yml` runs on pushes, pull requests and manual dispatch.
It uses Python 3.11 (the package minimum), with no provider credentials or paid
model calls. Jobs have read-only repository permissions and run serial tests
within each job. New runs cancel older runs for the same ref.

The `quality` job checks Ruff lint and formatting, strict mypy for both its
native Linux platform and Windows, and every
non-browser, non-desktop test. The `browser` job installs Chromium and its
Linux dependencies, runs every browser test excluding Windows desktop tests,
using `--require-browser` (launch errors and zero executed browser tests fail),
then produces fresh benchmark and security reports. Desktop UI Automation
still requires a separate interactive Windows session.

The browser tests include a real `cua doctor --probe-browser` launch/close and an
unauthenticated application HEAD probe. The `installed` job checks doctor JSON
and browser probing in a non-editable wheel outside the checkout, alongside
template/resource checks and the full demo.

The hosted browser job explicitly sets `CUA_CHROMIUM_NO_SANDBOX=1` for detached
Chromium: hosted Linux runners can restrict the user namespaces its sandbox
requires. Detached browsers keep their sandbox enabled outside this opt-in.
Vision tests capture fixture controls on their current platform because native
fonts and form controls differ between Windows and Linux; match thresholds
and refusal assertions remain unchanged.

The benchmark smoke runs only deterministic replay: two independent runs each
of successful lookup, unknown-member lookup and a write without consent.
`scripts/ci/check_benchmark.py` requires a single completed scripted session,
all six exact scores, unique run IDs, no cached invocations, no model calls,
no commits and no duplicate side effects. Missing or malformed evidence fails
the gate. This small smoke checks runtime correctness; it does not estimate
live-model quality, latency improvements or statistical significance. The
browser integration suite separately exercises the scripted baseline,
discovery, idempotency and duplicate-commit scoring.

The full security benchmark runs even if an earlier browser step fails,
unless the job was cancelled. Its existing CLI fails if any scenario is not
blocked, including probe errors, secret exposure and approval/policy bypasses.
The browser tests also include negative controls with defenses disabled.

Artifacts are uploaded on success or failure: JUnit results, raw benchmark
metrics/session metadata, and Markdown/JSON benchmark and security reports.
Browser artifacts expire after 14 days. Raw screenshots, traces, session
plumbing and security canaries are excluded. Reports write under gitignored
`artifacts/`, preserving committed benchmark reports. A failure before report
generation can leave only test results; inspect the job log in that case.

Reproduce the artifact steps from the repository root after installing
`.[dev]` and Playwright Chromium:

```sh
python -m cua.cli benchmark run --suite core --llm scripted --strategy inter_cua_replay --repetitions 2 --task lookup-success --task lookup-unknown-member --task open-without-consent --reports-dir artifacts/benchmark
python -m cua.cli benchmark report --latest --reports-dir artifacts/benchmark
python scripts/ci/check_benchmark.py artifacts/benchmark
python -m cua.cli security run --out artifacts/security --runs-root bench/security/runs
```

Use a fresh reports directory: the smoke gate intentionally refuses appended
sessions. Repository administrators must configure branch protection to require
the `quality` and `browser` checks; a workflow file cannot enforce that setting.
