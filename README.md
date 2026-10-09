# inter-cua

Discover a task in an application UI once, review the resulting capability, then
call it by name through deterministic replay. For example, an agent can request
`member_savings_balance(member_id="10003")` and receive balance `1411.21` without
asking a model to navigate the application again.

Discovery produces a **draft**. A reviewer describes and approves its exact content.
Replay uses no model and returns `success`, `business_outcome`, `failure`, or
`escalated`. A supported browser session can be handed to a person when automation
cannot safely continue. Writes also require invocation-specific consent; artifact
approval alone does not authorize a commit.

```text
Goal → discovery → draft → describe / approve → registered capability
                                                   ↓
CLI / HTTP / MCP / workflow → deterministic replay → typed result + evidence
                                                   ↓
                                            human takeover / resume
```

## Supported targets

- **Browser:** Playwright Chromium, including frames and accessibility-based controls.
- **Native desktop:** Windows UI Automation in an interactive Windows session,
  with the `windows` extra. Desktop screenshots, traces, vision and session handoff
  are unavailable; unsupported feature requests are refused before action.
- Bundled examples are a synthetic legacy banking UI and Windows DeskCalc.
  Other applications need their own tenant, policy, family and reviewed capability.

Native macOS/Linux adapters, additional browsers and remote browser viewing are
future extensions. See [platform support and limitations](docs/platforms.md).

## Installation and no-key demo

Use Python 3.11 or newer. The demo uses packaged resources and also runs from a
non-editable wheel outside the checkout. Playwright's Chromium binary is installed separately
from the Python package. The short demo starts its own services and runs all seven
stages with three independent replays; no provider key or `.env` setup is needed.

PowerShell (Windows):

```powershell
git clone https://github.com/aniaisec/inter-cua.git
Set-Location inter-cua
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m cua.cli demo --repetitions 3
```

POSIX shell (Linux/macOS browser environment):

```sh
git clone https://github.com/aniaisec/inter-cua.git
cd inter-cua
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m playwright install chromium
.venv/bin/python -m cua.cli demo --repetitions 3
```

On Debian/Ubuntu, install the matching Python venv package if `python3 -m venv`
reports that `ensurepip` is unavailable (for example `sudo apt install python3-venv`).
On Linux, missing system browser libraries may require
`.venv/bin/python -m playwright install --with-deps chromium` with permission to
install OS packages. For later CLI examples, activate with
`.\.venv\Scripts\Activate.ps1` in PowerShell or `. .venv/bin/activate` in POSIX.
If PowerShell activation is restricted, use the environment's executable directly,
for example `.\.venv\Scripts\python.exe -m cua.cli --help`.

To create an editable project after installation, run
`cua init my-project --template demo`, then read `my-project/README.md`.
The `web` and `windows` templates provide application-specific starting
configuration. Run `cua --root my-project doctor --tenant local` to check setup;
add `--probe-browser` to test Chromium launch or `--json` for a machine report.
Add `--dry-run` to init to preview files without writing. Existing files
are refused; no capability is automatically approved.

The base install includes browser replay, HTTP/MCP, scripted discovery and the
demo. Live discovery additionally needs the selected provider SDK and your key:
install `".[anthropic]"` for Claude, `".[gemini]"` for Gemini, or
`".[discovery]"` for both from this checkout. Add `vision` for screenshot matching
or `windows` for UI Automation. Developers can install `-e ".[dev]"`;
that extra includes both provider SDKs, vision and the test/build tools.

Run `cua init` or `cua demo` from any directory; the package supplies starter
YAML/JSON, scripts, and mock HTML. See [packaging and release gates](docs/packaging.md)
for wheel/sdist builds, independent extras, fresh-install checks and pinned
Python 3.11 development dependencies. `cua --version` reports the package version.

A successful run writes `summary.md`, `summary.json`, a masked command manifest and
evidence under a fresh `demo/demo_<session-id>/`. Expect three successful replays,
zero replay model calls, one approved commit and one blocked hostile-page attack.
Discovery, review and takeover are scripted in this demonstration; it validates
the mechanism, not live model planning or a person's effort.
See [getting started](docs/getting-started.md), the [complete demo guide](scripts/demo/README.md)
and [troubleshooting](docs/troubleshooting.md).

## Results and evidence

An ordinary replay prints JSON. A successful lookup includes these fields
(excerpt; the full result also carries timing, run identity and evidence):

```json
{"kind": "success", "outputs": {"savings_balance": "1411.21"}, "side_effect": "none"}
```

| Exit | Replay result | Meaning |
|---|---|---|
| 0 | `success` | The operation completed with typed outputs |
| 2 | `business_outcome` | A declared answer such as `NOT_FOUND` |
| 1 | `failure` | Inspect the code, evidence and side effect |
| 3 | `escalated` | A person or consent is needed; retain the resume token |

Usage/configuration errors generally exit 64; argparse errors exit 2. Decimal
outputs are strings. If `side_effect` is `unknown`, reconcile with the application
before repeating a write. Preserve an idempotency key across retries.

Normal discovery and replay evidence defaults to `evidence/runs/<run-id>/`.
Discovery records model calls; replay uses none. Preflight refusals may create no
run directory. See [CLI contracts](docs/cli.md) and [operations](docs/operations.md)
for result fields, handoff, consent and evidence handling.

## Automate your application

Follow [your first application](docs/your-first-application.md) to create a tenant
binding, restrict the policy, define the product's family template, discover a
read-only operation, review it and test a second input and failure. Start with
`cua init PATH --template web`, edit its configuration, then run `cua doctor`.
See [configuration and secret precedence](docs/configuration.md) and
[diagnostic findings](docs/troubleshooting.md#doctor-findings).

Live discovery needs an Anthropic or Gemini provider key and incurs charges.
Scripted discovery needs a target-specific tool-call script. Replay, operator and
routine tests need no provider key. A happy-path run cannot infer all business
outcomes or recovery conditions.

## Choose an entry point

| Entry point | Use it for | Guide |
|---|---|---|
| CLI / catalog | Shell commands and subprocess callers | [CLI](docs/cli.md), [caller contract](docs/integrations.md#cli-callers) |
| HTTP | Shared local service with authorization and polling | [HTTP](docs/integrations.md#http) |
| MCP | Approved capabilities as stdio tools for an agent | [MCP](docs/integrations.md#mcp) |
| Workflows | Compose approved operations with typed bindings | [Workflow semantics](docs/architecture.md#workflows) |
| Operator / resume | Consent and takeover of the same browser session | [Operations](docs/operations.md) |

## Reference and evaluation

The [documentation index](docs/index.md) links configuration, troubleshooting,
architecture, extensions, operations and tests. [CI gates](docs/CI.md) describe
quality, strict browser verification and benchmark/security artifacts.

In the [paired synthetic evaluation](docs/evaluation.md), replay used zero model
calls and lower latency with a similar exact-result rate; it stopped safely on
renamed controls. The evaluation covers one mock application, excludes rerun
provider failures, and does not establish performance on other applications.
[Measured results and qualifications](docs/evaluation.md),
[benchmark reproduction](bench/README.md) and [curated evidence](evidence/README.md)
retain the complete tables and cost/human assumptions.

[Architecture](docs/architecture.md) explains approval, tenant isolation, consent,
idempotency, drift and recovery. See the [threat model](docs/THREAT_MODEL.md) and
[security reporting policy](SECURITY.md). [REPORT](REPORT.md) and
[phase history](docs/IMPLEMENTATION_PROGRESS.md) are historical design snapshots.

## License

MIT.
