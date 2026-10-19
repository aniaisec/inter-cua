# Contributing

Start with the [documentation index](docs/index.md), [architecture](docs/architecture.md)
and [roadmap](ROADMAP.md). Small fixes, reproducible failures and improvements to
the first-run guides are useful contributions. For a new adapter, provider or
schema feature, open a proposal describing the target, contract and verification
before investing in a large implementation. The [extension recipes](docs/extending.md)
identify the integration points.

Report vulnerabilities through the [private security channel](SECURITY.md#reporting-a-vulnerability).
Use public issues for ordinary bugs and proposals; see [support](SUPPORT.md) for
the information needed to reproduce a problem and current platform commitments.

## Set up a checkout

Use Python 3.11 or newer in a fresh environment. The following installs the
provider-free `test` extra used by routine CI. No provider key or `.env` is
required for tests. Run commands from the repository root.

PowerShell:

```powershell
git clone https://github.com/aniaisec/inter-cua.git
Set-Location inter-cua
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
git switch -c codex/my-change
```

POSIX shell:

```sh
git clone https://github.com/aniaisec/inter-cua.git
cd inter-cua
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
git switch -c codex/my-change
```

If activation is restricted, run the environment's Python directly, for example
`.\.venv\Scripts\python.exe -m pytest ...` on Windows. For the pinned Python 3.11
environment, add `-c constraints/dev-py311.txt` to the install command; those pins
are specific to 3.11. Other supported versions resolve the declared ranges.

Install Chromium only when running browser checks:

```sh
python -m playwright install chromium
```

Linux may need `python -m playwright install --with-deps chromium` and permission
to install system packages. Provider translation work needs `.[dev]`, which adds
both SDKs; desktop work needs `.[test,windows]` on interactive Windows.
See [packaging](docs/packaging.md) for independent extras and fresh wheel checks.

## Choose checks for the change

Run the smallest checks that establish the changed behavior locally. Explain
which checks ran, their results, and any unavailable environment in the PR.
CI still runs its assigned [compatibility gates](docs/CI.md) on every PR.

| Change | Local verification |
|---|---|
| Documentation, issue/PR templates | Check relative links, anchors and YAML; run changed command examples in a disposable project. Pure prose edits need no browser, desktop or paid-provider run. |
| Python implementation | Ruff, formatting, mypy and focused regressions; use the routine unit gate below for cross-module changes. Include a regression that demonstrates a behavioral bug before the fix. |
| Family, recorder, detector or recoverer | Recorder/schema/replay unit checks, then the relevant real target scenario. Cover detector scope, business vs technical outcomes, exhausted recovery and no repeated commit. |
| Surface, locator or feature vocabulary | Surface compatibility/unit checks plus the inherited live contract for the adapter. Test both advertised features and refusal of unavailable ones. UIA requires the manual Windows gate. |
| Policy, secrets, approval, consent or idempotency | Relevant safety/unit tests and the security gate; assert refusal before action and inspect independent commit/secret-exposure evidence. |
| CLI, HTTP, MCP, workflow or examples | Focused caller tests and affected integration scenarios. Preserve typed results, exit/status codes, keys and escalation behavior. |
| Packaging, dependencies, init resources or project paths | Packaging/initialize/project tests, wheel/sdist content checks and a clean non-editable installation outside the checkout. |
| Optional provider | Synthetic provider tests with sockets refused, missing-SDK checks and provider-free replay/import checks. Paid live calls are optional and never a routine PR prerequisite. |

In the activated environment, these commands work in either shell:

```sh
python -m ruff check .
python -m ruff format --check .
python -m mypy
python -m mypy --platform win32
```

The routine provider-free unit gate is:

```sh
python -m pytest -m "not browser and not desktop and not provider" --ignore=tests/unit/test_llm_providers.py
```

Focused checks for common changes:

```sh
python -m pytest tests/unit/test_recorder.py tests/unit/test_artifact_schema.py tests/unit/test_replay.py
python -m pytest tests/unit/test_surface_compat.py tests/unit/test_locators.py
python -m pytest tests/unit/test_example_clients.py tests/unit/test_example_processes.py
python -m pytest tests/unit/test_packaging.py tests/unit/test_initialize.py tests/unit/test_project.py
```

Run applicable commands, not every row. Browser behavior must use strict mode:

```sh
python -m pytest tests/integration/test_surface_contract.py --require-browser
python -m pytest tests/integration/test_examples.py --require-browser
python -m pytest tests/security --require-browser
```

Use `python -m pytest tests/unit/test_llm_providers.py` in a `.[dev]` environment
for provider translation. [Testing](docs/testing.md) describes the rest of the
suite; [CI](docs/CI.md) has critical branch coverage, the full security CLI and
the live desktop gate; [packaging](docs/packaging.md) has distribution commands.
A skipped GUI case or a Windows type check does not establish live UI behavior.

## Implementation and review

Keep changes focused and use the existing discovery, review and replay paths.
Preserve these invariants:

- Discovery records drafts; approval binds to exact reviewed content. Changed
  content must be described and approved again, including evaluated drift repairs.
- Replay makes zero model calls. Provider SDKs remain optional and lazily imported.
- Invalid inputs, denied policy and unsupported features fail before UI action.
  Ambiguous or stale references never select an arbitrary control.
- Business outcomes remain distinct from technical failures and escalation.
- An irreversible step is never retried. Preserve idempotency identity, and
  reconcile uncertain writes before another attempt.
- Secrets stay in tenant-bound references. Mask evidence before capture and
  scrub logs; use synthetic credentials in fixtures and reports.

Schema and feature vocabularies are closed contracts. Coordinate changes to
validation, requirement inference, adapters, exported schemas, packaged resources
and conformance probes. Keep old golden artifacts and approval/hash compatibility
unless a separately explained migration changes that contract. See
[extension verification](docs/extending.md#review-and-verification).

Use deterministic fixtures and independent state/commit oracles. Let scenarios
allocate ports, isolate runtime state and reap their subprocesses. Avoid adding
external live services, paid calls or committed generated keys as test prerequisites.
Do not regenerate curated evidence merely for a prose edit.

## Submit a pull request

Describe the concrete problem and resulting behavior for a reviewer who has not
seen the issue discussion. Include relevant validation and any compatibility or
approval implications. Update user-facing documentation and the `Unreleased`
section of [CHANGELOG.md](CHANGELOG.md) when behavior or contributor workflows change.

Review `git diff --check`, the diff and untracked files before submitting. Keep
`.env`, keys, tokens, private screenshots/traces, local review receipts and runtime
directories out of the PR. Use a minimal synthetic reproduction for public reports;
the [support guide](SUPPORT.md#reporting-a-problem) lists safe diagnostic fields.

Building a distribution does not publish a release. Follow the
[adoption release procedure](docs/release/adoption.md) to collect candidate gate
evidence and two independent developer trials before release review.
