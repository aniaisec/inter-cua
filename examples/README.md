# Runnable integration examples

Each directory has a purpose, prerequisites, one command, expected results,
cleanup instructions and a `scenario.json`. The runner and integration tests
consume those scenarios. No paid provider key is needed.

| Example | Contract exercised |
|---|---|
| [Lookup](lookup/README.md) | Typed CLI/catalog answer, zero replay model calls |
| [Business rejection](business_rejection/README.md) | Declared outcome, no write |
| [Approved submission](approved_submission/README.md) | Input-bound consent, one observed commit, same-key retry |
| [Drift and repair](drift_repair/README.md) | Refusal, evaluated draft, review still required |
| [Human takeover](human_takeover/README.md) | Automation pauses, live operator action, validated hand-back |
| [Workflow](workflow/README.md) | Typed output/input binding, stop after business rejection |
| [HTTP client](http_client/README.md) | Poll states, preserve request/key, handle conflicts |
| [MCP setup](mcp_setup/README.md) | Absolute verified paths, unrelated cwd, approved tools |
| [Windows UIA](windows_uia/README.md) | Interactive desktop, unsupported features refused |

From the repository root, install `python -m pip install -e .` and
`python -m playwright install chromium`. These commands work in PowerShell and
POSIX. The examples run using this checkout's example code and installed
inter-cua entry points; they are not a new packaged CLI command.

Run every browser example with a new output directory:

```sh
python -m examples.run all --out artifacts/examples/run1
```

Run a single example using its directory name:

```sh
python -m examples.run human_takeover --out artifacts/examples/handoff1
```

The runner creates a separate project with spaces in its path, starts its own
loopback target on an allocated port, reviews synthetic draft capabilities,
and executes from an unrelated working directory. Each write check captures
the app's independent oracle after discovery and asserts the subsequent commit
count and submitted commit requests. This demonstrates sequential retry safety;
durable recovery after process loss belongs to PR12-14.

Each output directory retains `summary.json`, `commands.json`, a text terminal
recording (`demonstration.cast`, asciicast v2) and the project's replay evidence.
The handoff recording shows review, escalation, evidence paths and the result
after an explicitly scripted operator acts through the ordinary console. Read
the JSON without a player, or play the cast with an asciicast-compatible viewer.
Video is optional; all behavior is also documented in text. Tokens are omitted
from the recordings. Private per-project signing keys remain inside the generated
project: share the recording/evidence you have inspected, not the whole project.

Servers and live sessions are closed on completion or failure. Output directories
are never overwritten; delete only a run's chosen output directory after review,
or select another name. Repeated runs need no manual reset.

Verification:

```sh
python -m pytest tests/unit/test_example_clients.py tests/unit/test_example_processes.py
python -m pytest tests/integration/test_examples.py --require-browser
```

Windows UIA has a separate live gate. In an unlocked interactive Windows session,
install `python -m pip install -e ".[windows]"` and run:

```powershell
python -m examples.run windows_uia --out artifacts/examples/windows1
python -m pytest tests/desktop/test_examples.py
```

`all` excludes desktop unless `--include-desktop` is supplied. A browser/type
check does not establish live desktop support. The existing
[seven-stage demo](../scripts/demo/README.md) remains the comprehensive regression
scenario. For creating your own app configuration, start with the
[inventory tutorial](inventory/README.md).
