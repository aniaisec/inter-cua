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

[Documentation index](index.md) · [Project README](../README.md)
