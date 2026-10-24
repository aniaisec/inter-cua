# CI gates and artifacts

`.github/workflows/ci.yml` runs on pushes, pull requests and manual dispatch.
Linux compatibility covers Python 3.11 (the minimum) and 3.14. Python 3.11 uses
`constraints/dev-py311.txt`; the newer version and fresh consumer installations
resolve declared dependency ranges. Tests run serially inside each job. Jobs
have read-only repository permissions and newer runs cancel older runs for the
same ref.

| Job | Acceptance gate |
|---|---|
| `quality` (Linux 3.11/3.14) | Ruff, both mypy platforms, provider-free unit tests and critical branch coverage |
| `browser` (Linux 3.11/3.14) | Strict Chromium regressions, shared Surface contract and deterministic benchmark smoke |
| `examples` (Linux 3.11) | Every browser example's documented scenario and repeated writes |
| `security` (Linux 3.11) | Offline/live probes, negative controls and a fresh full security report |
| `windows` (Windows 3.14) | Focused paths, parsing, packaging and feature tests; real Chromium, inventory and shared Surface contract |
| `providers` (Linux 3.11) | Optional SDK interoperability with synthetic keys and sockets refused |
| `installed` | Fresh wheel/sdist, minimum/newer Python, optional extras and packaged resources |
| `service-recovery` (Linux/Windows 3.14) | Named synthetic recovery exercises and strict JUnit checks; local records retain pending manual qualification gates and are not uploaded |

Routine jobs install `.[test]`, which includes test tools and vision support
without either paid provider SDK. `check_no_providers.py` fails if one is present.
Provider tests live in `tests/unit/test_llm_providers.py`; routine unit and browser
jobs ignore that file during collection. The `providers` job installs `.[dev]` and
runs it separately. No job receives provider credentials or calls a live model.
Provider constructor checks in the installed matrix also forbid network calls.

Release report helpers are type-checked separately from the package. The manual
adoption [readiness checker](release/adoption.md) is included in that type check;
its rejection tests run in the quality unit suite. The adoption snapshot preparer
is type-checked there too; tests verify exact committed source, failure retention,
safe extraction and pending review gates. Human trial records remain a
manual release requirement and are not manufactured by CI. The manual
desktop gate uses an explicit `--platform win32` target even on Linux runners,
so mypy checks its Windows execution path rather than rejecting its platform guard
as an unreachable statement.

The browser and example gates use `--require-browser`: missing/unusable Chromium
and zero executed browser cases fail. Examples and security have their own jobs
and reports. The result checker rejects empty reports, failed/collection-error
reports and unexpected skips. Raw screenshots, traces, session plumbing,
generated keys and security canaries are excluded from uploaded artifacts.

The hosted Linux jobs opt into `CUA_CHROMIUM_NO_SANDBOX=1` for detached Chromium
because those runners can restrict user namespaces. Local detached browsers
keep their sandbox enabled without this opt-in. Vision fixtures capture controls
on the current platform; refusal criteria and match thresholds stay the same.

Admission saturation, queue expiry/shutdown and bounded MCP dispatch tests run
in the Linux unit suites and focused Windows job. They check retry preservation,
absence of claims for overflow, no dispatch after expiry, and responsive ping
and status while work-call capacity is exhausted. The HTTP browser integration
holds a real session open to check that overflow opens no extra browser.

Atomic-persistence fault and subprocess-lock tests run in both Linux quality
jobs and the focused Windows job. They cover interrupted publication, corrupt
authority, competing thread/process updates, lock release on process death and
HTTP/MCP refusal before dispatch. These checks establish file integrity, not
durable ownership of an invocation or exactly-once application effects; see
[operations and reconciliation](operations.md#corrupt-or-interrupted-state).

## Surface conformance

`tests/contracts/surface.py` supplies the same inherited suite to real Chromium
and Windows UIA. Each adapter fixture owns a controlled target and verifies its
independent browser endpoint/window oracle in `finally`, including assertion failures.

The suite checks observation-scoped refs, ambiguity and measured fallback,
viewport mismatch refusal, frame/location scoping, condition evaluation and
timeouts, typed forms, keyboard and native dialogs. Optional probes cover masked
screenshots, guarded pointer input after an external page change, document
status, egress and published sessions. A feature-to-probe map must cover the
closed feature vocabulary. A claimed feature failing its behavioral probe fails
the corresponding browser or manual desktop gate.

```sh
python -m pytest tests/integration/test_surface_contract.py --require-browser
```

## Live Windows release gate

Hosted Windows CI checks Chromium and configuration; it does not run Windows
UIA. A type check for Windows also cannot establish live desktop support.
Before releasing desktop support, use a reviewed checkout in a dedicated,
unlocked interactive Windows session with PowerShell/WinForms:

```powershell
python -m pip install -e ".[test,windows]"
python -m scripts.ci.run_desktop_gate --out artifacts/desktop/release1
```

Choose a new output directory. The gate refuses other platforms or a missing
Windows extra, runs the complete desktop suite with `--require-desktop`, and
checks its JUnit execution report. Zero executed tests, test failures and skips
for missing prerequisites fail. Only explicit "adapter does not claim ..."
skips for optional features are allowed. `desktop.xml` and `summary.json` report
executed/skipped counts and skipped case names. Retain those reports as release
evidence; runtime projects and private keys stay local.

The PR09 local gate executed 29 desktop cases and skipped three optional browser
features (guarded pointer, HTTP document status and egress). The same tests also
exercise refusal of screenshots and session handoff on UIA. These counts are
evidence for that run, not assumptions about a future machine.

No self-hosted desktop runner is exposed by this workflow. If one is introduced,
keep it isolated from untrusted pull requests and unrelated desktop sessions.

## Critical branch coverage

The unit job collects branch coverage, writes `artifacts/coverage.json` and
checks five safety functions: JSON input coercion, scalar validation, nested
placeholder binding, artifact-only credential expansion and pixel-environment
refusal. Each must have zero missing statements or branches. Missing functions,
empty evidence and reports without branch collection fail the gate. There is no
blanket percentage requirement for the whole package.

Coverage discovers source by directory and limits the report to these modules.
Using importable module names for discovery can pre-import Pydantic models and
break decimal serialization on Python 3.14. The directory-based configuration
keeps the same statement and branch requirements without those import side effects.

A unit-only baseline measured on October 13, 2026 with provider-free Python
3.11 on Windows gives the following combined statement/branch percentages:

| Module | Measured coverage |
|---|---|
| `cua.catalog` | 97% |
| `cua.project` | 100% |
| `cua.replay.invocation` | 98% |
| `cua.replay.engine` | 70% |
| `cua.surface.locators` | 91% |

The five gated functions cover all 36 branches and 51 statements. Other replay
and adapter paths also have live browser/desktop tests, which this unit-only
measurement does not include. Generated adversarial cases cover input types,
regex/template values, path/cwd combinations and malformed capability/ledger
records. The placeholder regressions prove that supplied input and credential
contents remain literal instead of becoming another template.

```sh
python -m coverage run -m pytest -m "not browser and not desktop and not provider" --ignore=tests/unit/test_llm_providers.py
python -m coverage json -o artifacts/coverage.json
python scripts/ci/check_coverage.py artifacts/coverage.json
```

## Installed packages and reports

The installed matrix inspects wheel/sdist metadata and resources, then creates
a fresh non-editable environment outside the checkout with `PYTHONPATH` removed.
Base wheel checks cover Linux 3.11/3.14; sdist-built wheel checks cover 3.11.
They exercise doctor, browser launch, the seven-stage no-key demo and inventory
discovery/review/consent/retry with provider SDKs absent. Other entries check
`anthropic`, `gemini`, `discovery`, `vision` and Windows dependency/adapter
imports. The Windows installed entry does not replace the live desktop gate.
See [packaging commands](packaging.md).

Artifacts upload on success or failure. Quality stores unit JUnit and coverage
JSON; browser jobs store JUnit and benchmark metrics/reports; example, Windows
and provider jobs store their JUnit; security stores JUnit and summary reports.
Browser/security reports expire after 14 days. A failure before report creation
can leave only logs or partial artifacts, which cannot certify a passing gate.

The deterministic benchmark smoke checks two independent replays each of a
lookup, an unknown-member answer and a write without consent. Its existing
checker requires one completed scripted session, six exact scores, unique run
IDs, no cache hits, no model calls and no commits. This does not measure live
model quality or performance. The browser suite separately exercises the
scripted baseline, discovery and duplicate-commit scoring.

The security CLI fails if an attack is not blocked, including probe errors,
secret exposure and approval/policy bypass. It runs even after a preceding
security test fails, unless cancelled. Negative controls prove attacks succeed
when their defenses are removed.

The existing [PR07 local helper](../scripts/ci/test_pr07.ps1) remains available
for inventory and installed-package verification. New compatibility and release
gates are described above. Repository administrators must require each selected
matrix check in branch protection; a workflow cannot enforce that setting.
