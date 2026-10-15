# Support policy

inter-cua is alpha software. Support and security fixes target the latest commit
on `main`; older commits and package snapshots have no backport commitment.
The development version is `0.1.0`. A version number or successful local build
does not imply that the adoption release gates have been completed.

Support is best effort through [GitHub issues](https://github.com/aniaisec/inter-cua/issues).
There is no guaranteed response time or release schedule. Report vulnerabilities
through [private vulnerability reporting](https://github.com/aniaisec/inter-cua/security/advisories/new)
as described in [SECURITY.md](SECURITY.md), including suspected approval bypass,
credential exposure, cross-tenant access or unintended UI commits.

## Supported environments

| Area | Current boundary and verification |
|---|---|
| Python | Requires 3.11 or newer; classifiers list 3.11–3.14. Linux CI runs 3.11 and 3.14. Intermediate versions have no separate CI job. |
| Browser | Playwright Chromium. Linux 3.11/3.14 and focused Windows 3.14 browser checks run in CI. Chromium's binary and OS libraries are installed separately. |
| macOS | Browser use is within the Playwright Chromium target boundary, but this repository has no macOS CI job. Native macOS control is unavailable. |
| Windows desktop | UI Automation with the `windows` extra in an unlocked interactive Windows session. Hosted Windows CI does not verify UIA; a separate live manual release gate is required. |
| Live discovery | Optional Anthropic or Gemini SDK and a locally configured key. Select a provider/model available to your account; default model availability is not guaranteed. Routine CI uses synthetic responses and no paid calls. |
| Shared service | Current HTTP/MCP and local operator behavior only. Durable cross-process invocation ownership, bounded admission, authenticated operator roles and recovery hardening are planned. |

The [platform guide](docs/platforms.md) lists adapter features and refusal behavior;
[CI evidence](docs/CI.md) distinguishes automated coverage from the live desktop
gate. Native Linux control, other browsers, remote browser viewing and distributed
workers are outside the current boundary. See [operations](docs/operations.md)
before operating a shared service and the [roadmap](ROADMAP.md) for planned work.

## Reporting a problem

Start with [troubleshooting](docs/troubleshooting.md). Use the bug-report template
and include only the information needed to reproduce the failure:

- `cua --version`, Python version, OS, installation method and optional extras;
  for a checkout, its commit SHA.
- A minimal command or caller request with secret values and headers removed,
  the selected root/tenant/adapter, expected behavior and actual result/exit code.
- Relevant finding/error codes from `cua --root DIR doctor --tenant local --json`;
  state whether browser/application probes were explicitly enabled.
- A synthetic reproduction, affected capability/schema version and the result's
  `kind`, `code` (when present) and `side_effect`. Include executed/skipped counts
  if the issue concerns test coverage.

Doctor omits secret values, but local paths, tenant names, application URLs and
artifact metadata may still identify your environment. Review even masked output
before sharing. Do not upload `.env`, secret files, API/approval/resume tokens,
authorization headers, entire run directories or raw screenshots/traces. Keep
original evidence privately when diagnosing a possible write.

If `side_effect` is `unknown`, a process was interrupted during a write, or the
API reports a run `lost`, reconcile the application's actual state first. Preserve
the request's idempotency key and evidence; do not generate a new key to force a
repeat. The [operations guide](docs/operations.md) explains the current limits.

Feature proposals should describe a concrete application, needed behavior,
current workaround and how correctness can be verified. For code contributions,
follow [CONTRIBUTING.md](CONTRIBUTING.md).
