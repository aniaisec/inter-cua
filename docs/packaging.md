# Packaging and release gates

Python 3.11 or newer is required. The package version comes from
`src/cua/__init__.py`; Hatch reads that source for both wheel and sdist metadata.
`cua --version` and `cua.__version__` use the same value. Update that file and
[CHANGELOG.md](../CHANGELOG.md) together when preparing a release. Building and
testing archives does not publish them; publication is a separate release action.

## Installation combinations

| Install | Included functionality |
|---|---|
| Base | Browser replay, HTTP/MCP, scripted discovery, init, doctor and no-key demo |
| `anthropic` | Claude SDK for live discovery |
| `gemini` | Google Gen AI SDK for live discovery |
| `discovery` | Both provider SDKs |
| `vision` | NumPy and Pillow for screenshot matching |
| `windows` | Windows-only comtypes for UI Automation |
| `dev` | Both SDKs, vision, tests, typing, lint and distribution build tools |

Provider SDKs import only when the provider is selected. Selecting one without
its SDK fails before launching the UI and names the extra to install. Provider
keys remain necessary for live discovery; replay never selects a model. Auto
selection preserves the configured preference, then Anthropic key precedence.
It reports a missing selected SDK rather than silently choosing another provider.

From this checkout, install `.` for base, `".[gemini]"` for one provider, or
`".[anthropic,gemini,vision]"` to combine extras. For editable development use
`-e ".[dev]"`; add `windows` on an interactive Windows machine as needed.
For an already built wheel, use the exact same wheel path with extras:

```sh
python -m pip install "dist/inter_cua-0.1.0-py3-none-any.whl[gemini]"
python -m playwright install chromium
```

Substitute the archive version you built. Once a release is published, its
equivalent named installation is `"inter-cua[gemini]==VERSION"`. Do not mix
different source and published versions when adding extras. Playwright browser
binaries and Linux browser libraries are installed separately. Windows imports
do not establish readiness of an interactive desktop; see [platforms](platforms.md).

## Reproducible Python 3.11 development

Library metadata keeps compatible dependency ranges. The separate
`constraints/dev-py311.txt` records an exact development dependency snapshot,
including test/build tools and provider SDKs. CI quality, browser and build jobs
apply it as constraints; it does not add provider SDKs to the base distribution.
Fresh consumer gates deliberately install without these constraints to test the
declared ranges. The snapshot does not pin browser operating-system libraries,
target applications or remote models.

Create a fresh Python 3.11 virtual environment, activate it, then run:

```sh
python -m pip install -c constraints/dev-py311.txt -e ".[dev]"
python -m playwright install chromium
```

Use that environment for tests and deterministic benchmarks. `windows` can be
added to the install on Windows; unused platform-specific constraints do not
force packages onto Linux. Other Python versions use the declared ranges instead
of this Python 3.11 snapshot. To intentionally refresh it, create another fresh
Python 3.11 environment and resolve without constraints:

```sh
python -m pip install -e ".[dev,windows]"
python scripts/ci/lock_dev.py constraints/dev-py311.txt
```

Review the resulting dependency changes and rerun all CI gates. The generator
records package names/versions and excludes the editable project itself, so it
does not put machine paths into the snapshot.

## Wheel and sdist verification

From the pinned development environment at the repository root, with a fresh
`dist` directory:

```sh
python -m build --no-isolation --outdir dist
python scripts/ci/check_distribution.py dist/inter_cua-0.1.0-py3-none-any.whl dist/inter_cua-0.1.0.tar.gz
python scripts/ci/verify_installed.py dist --browser
python scripts/ci/verify_installed.py dist --from-sdist --browser
python scripts/ci/verify_installed.py dist --extra anthropic
python scripts/ci/verify_installed.py dist --extra gemini
python scripts/ci/verify_installed.py dist --extra discovery
python scripts/ci/verify_installed.py dist --extra vision
```

On Windows also run `python scripts/ci/verify_installed.py dist --extra windows`.
On Linux install browser system dependencies beforehand with
`python -m playwright install --with-deps chromium`. Archive names above are
examples; substitute the version actually built.

The inventory gate checks version consistency, provider extras, URLs and Python
metadata, starter dotfiles/TOML/YAML/JSON/schema/scripts, and mock HTML in both
archives. Signing keys and review receipts are refused. Installed checks create
and remove their own isolated virtual environment and scratch project, clear
`PYTHONPATH`/`PYTHONHOME`, and run outside the checkout. The sdist path rebuilds
its wheel outside the checkout before installing it. SDK presence/absence and
lazy imports are asserted, and constructors cannot connect to a network.

Every combination runs CLI help/version, all three initializer templates,
static doctor JSON and the mock HTML endpoint. Base gates additionally launch
Chromium and run all seven demo stages: scripted discovery, describe/approve,
deterministic replay, drift, takeover, consent and security. Replay must record
zero model calls and the demo exactly one consented commit. Provider translation
is covered by deterministic unit tests; installed gates make no paid model calls.

[CI gates](CI.md) · [Documentation index](index.md) · [Changelog](../CHANGELOG.md)
