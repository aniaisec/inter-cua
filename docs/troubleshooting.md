# Troubleshooting

Run `cua doctor --tenant local` in the installed environment. Select a project
from anywhere with `cua --root DIR doctor --tenant local`. Add `--json` for a
machine-readable report. Static checks validate configuration, adapter/dependencies,
Chromium executables, tenant/policy/family, applicable capabilities and secret
sources. They create and remove temporary files to verify runtime directory write
access, removing any empty directories they created. They never create signing
keys, review receipts, or approvals, and never print secret values.

Chromium inspection uses the installed Playwright CLI's local `install --dry-run`
to find its expected binaries; it does not install, download, or launch a browser.
Add `--probe-browser` to launch and close local headless Chromium without visiting
an application. Add `--probe-app` to send an unauthenticated HEAD to the tenant
base URL with a five-second request timeout. Only an allowed URL is probed;
redirects are not followed, no login occurs, proxy environment credentials are
unused, and response bodies are not reported. A 401/403 or unsupported HEAD is a
warning because it does not prove that authentication or application health failed.
Desktop tenants have no browser/HTTP probes; interactive UIA readiness needs
manual verification. Exit 0 means no error findings in the checks performed,
1 means failed checks, and 64 means malformed invocation or project selection.
Use `cua surfaces`, `cua describe <capability>`, and run evidence for deeper checks.

## Doctor findings

The report includes a remedy for every finding. Common failed checks and their
next steps are listed here; `info` findings report successful checks, and warnings
include draft lifecycle status, empty catalogs, starter family placeholders,
optional dependencies, and unverified interactive desktop sessions.

| Code | Remedy |
|---|---|
| `INVOCATION_INVALID` | Read `cua doctor --help`; correct flags or missing values |
| `PROJECT_INVALID` | Select an existing root and correct version 1 `cua.toml` |
| `TENANT_INVALID` | Correct the selected tenant YAML and configured tenant directory |
| `POLICY_INVALID` | Correct policy YAML and compile its regex fields |
| `FAMILY_INVALID`, `FAMILY_SURFACE_MISMATCH` | Correct the configured family template and its target surface |
| `APPLICATION_LOCATION_INVALID`, `APPLICATION_LOCATION_PLACEHOLDER` | Set a real http(s) URL without embedded credentials, or a desktop `uia://` location |
| `APPLICATION_POLICY_BLOCKED`, `APPLICATION_PROBE_BLOCKED` | Allow the intended origin and probe path in a narrowly scoped policy |
| `SECRET_UNAVAILABLE` | Provide the named environment/file source in its declared format; doctor never creates keys |
| `DEPENDENCY_MISSING`, `UIA_DEPENDENCY_MISSING` | Install the dependency named in the finding; UIA needs Windows and `inter-cua[windows]` |
| `CHROMIUM_MISSING`, `BROWSER_EXECUTABLE_MISSING` | Run `python -m playwright install chromium` in the selected environment |
| `CHROMIUM_NOT_EXECUTABLE`, `CHROMIUM_CHECK_FAILED` | Check executable and Playwright process permissions, then repeat inspection |
| `BROWSER_LAUNCH_DENIED` | Use an environment allowed to start Chromium and check process permissions |
| `BROWSER_LAUNCH_FAILED`, `BROWSER_LAUNCH_TIMEOUT` | Check startup and system libraries; Linux may need `python -m playwright install --with-deps chromium` |
| `APPLICATION_UNREACHABLE`, `APPLICATION_TIMEOUT`, `APPLICATION_HTTP_ERROR` | Check the origin, running service, network, trusted certificates and HTTP status |
| `CAPABILITY_INVALID`, `CAPABILITY_DIRECTORY_INVALID` | Inspect artifact/registry files and correct invalid data as a draft |
| `CAPABILITY_INCOMPATIBLE` | Inspect `cua surfaces`; every required feature must be supported |
| `CAPABILITY_CREDENTIAL_INVALID` | Bind the credential to this tenant's application secret and declared fields; system secrets are forbidden |
| `CAPABILITY_APPROVAL_MISSING` | Review/approve the exact content or restore its trusted registry approval |
| `RUNTIME_NOT_WRITABLE` | Grant directory write/delete access or change the configured `runs`/`state` path |
| `BROWSER_PROBE_UNSUPPORTED`, `APPLICATION_PROBE_UNSUPPORTED` | Omit browser/HTTP probes for desktop tenants |

| Symptom | Likely cause | Action |
|---|---|---|
| `cua` not found / import failure | Wrong environment | Activate `.venv` or use its Python with `-m cua.cli` |
| Chromium executable missing | Browser not installed in this environment | Run `python -m playwright install chromium` |
| Chromium launch denied | Process/sandbox restriction | Read the original launch error; use an environment allowed to start Chromium |
| Tenant, policy or family missing | Wrong cwd or incomplete setup | Run from the root; inspect [path rules](configuration.md); create the family before discovery |
| `INPUT_INVALID` | Wrong name, type, pattern or non-finite decimal | Compare with the input contract; keep IDs as strings |
| Draft returns `POLICY_BLOCKED` | No exact-content approval | Describe, review, then approve; never edit approval fields by hand |
| Consent refused | Wrong tenant/inputs, expired or spent token | Obtain exact invocation consent; retain the original retry key |
| `AUTH_FAILED` | Missing or rotated credential | Check the tenant's secret source locally without printing its value |
| `SURFACE_INCOMPATIBLE` | Required feature unavailable | Inspect `cua surfaces` and [supported flags](platforms.md) |
| Locator/checkpoint failure | Drift or ambiguous controls | Inspect the masked screen and ladder; repair/re-record a draft and review |
| `escalated` | Person or consent required | Operate the same session; use its resume token or API run tools |
| Unknown side effect, API `lost` or interrupted write | Commit may have happened | Reconcile target state; preserve evidence/key; do not repeat blindly |
| HTTP 401 / 403 | Key, tenant, scope or token mismatch | Check access configuration and [headers](integrations.md#http) |
| HTTP 428 / 409 | Missing write key / conflicting reuse | Supply a stable key; do not reuse for changed inputs |
| Demo output already exists | Runner refuses overwrite | Omit `--out` for a fresh session or choose a new directory |
| Desktop tests skipped | Missing Windows/comtypes prerequisite | Use interactive Windows with the `windows` extra; skips are not proof of support |

Optional browser mode skips only a recognized missing executable. Release/CI checks
use [strict browser verification](testing.md#strict-browser-verification).
A demo's failed manifest and run result identify where to look; missing evidence
is not a passing stage. See the [demo output guide](../scripts/demo/README.md#output-and-failure-handling).

## Provider and model selection

Scripted discovery, replay and the no-key demo need no provider SDK or key.
For live discovery, select the provider explicitly while diagnosing:
`--llm anthropic` or `--llm gemini`. Install its extra in the same environment as
`cua`, using the same checkout, wheel or published version as the existing
installation; see [extra installation commands](packaging.md). From the checkout,
use `python -m pip install -e ".[anthropic]"`, `python -m pip install -e ".[gemini]"`
or `python -m pip install -e ".[discovery]"` for both. `cua --version` and
`python -m pip show inter-cua` help identify an unexpected environment. Doctor
does not contact a model provider or prove that an account has access to a model.

| Symptom | Next check |
|---|---|
| No model API key found | Set a key locally in the selected project's `.env` or process environment; do not paste it into a report. Anthropic accepts `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`; Gemini accepts `GEMINI_API_KEY`/`GOOGLE_API_KEY`. |
| Selected SDK missing even though another provider is installed | Install the selected extra. `auto` chooses from preference/keys and does not switch merely because that SDK is missing. |
| Unexpected provider | An explicit `--llm` wins; `auto` consults `CUA_LLM`, then available keys, preferring Anthropic if both are set. Check process environment as well as project `.env`. |
| Model unavailable, unknown or not authorized | Choose a model offered to your account with `--model MODEL_ID`. Otherwise Anthropic reads `CUA_MODEL`, and Gemini reads `CUA_GEMINI_MODEL`, before their defaults. Model names are provider-specific; defaults/aliases do not guarantee account access or reproducibility. |
| Authentication or quota/rate-limit error | Check the selected account, key validity and quota privately. A changed model name does not repair an invalid key. Respect the provider response; retries are bounded and cannot resolve authorization failures. |
| Persistent timeout/service error | Inspect the scrubbed exception and local network/proxy configuration. Record provider, requested model and actual response identity when present; avoid unbounded reruns of discovery. |

Process environment overrides the selected root's `.env`; changing that file
will not replace an already exported value. See [configuration precedence](configuration.md#environment-precedence).
Discovery evidence includes provider/model response identity and usage in
`model_calls.jsonl` when calls occurred. An alias can resolve to a different
model later; retain the actual version when available. Review evidence locally
before sharing excerpts. The [provider recipe](extending.md#add-a-discovery-provider)
describes synthetic interoperability checks.

## Locate the failure safely

1. Confirm the project and invocation: `cua --root DIR doctor --tenant local --json`,
   then `cua surfaces` and `cua describe CAPABILITY.json` as appropriate. Explicit
   file paths are relative to the calling directory, even with `--root`; named
   lookup uses configured project paths. A draft or changed approval needs review.
2. Read the result's `kind`, `code` when present, `side_effect`, run ID and exit
   code. `business_outcome` is an application answer, not a transport failure.
   `escalated` retains the existing session/resume path. Preflight refusals may
   have no run directory; a missing directory does not establish that UI work ran.
3. Inspect the selected run's `result.json` and event log locally. Normal runs
   use the configured runs directory (default `evidence/runs/<run-id>/`); demo
   runs also have `summary.json` and a masked command manifest. For drift, inspect
   scoped locators/checkpoints, repair a draft and repeat review rather than
   weakening ambiguity or approval checks.
4. For an interrupted write, `side_effect: unknown`, or an API `lost` state,
   preserve the evidence and original key. Inspect the target's actual state
   before another action. Current file persistence does not provide durable
   cross-process ownership; a fresh key or process restart cannot prove a write
   was absent. Follow [operations](operations.md) for reconciliation and service
   boundaries. Avoid deleting run/state files as a retry workaround.

If the failure remains, use the [bug-report form](https://github.com/aniaisec/inter-cua/issues/new?template=bug_report.yml)
with a synthetic reproduction and reviewed diagnostic codes. The
[support policy](../SUPPORT.md#reporting-a-problem) lists useful fields and private
material to omit. Send suspected security failures through the
[private vulnerability channel](../SECURITY.md#reporting-a-vulnerability).

[Documentation index](index.md) · [Project README](../README.md)
