# Troubleshooting

Run diagnostic commands from the root in the installed environment. There is no
`cua doctor` yet. Begin with `cua --help`, `cua surfaces`, `cua describe <capability>`
and the invocation result / stderr.

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
