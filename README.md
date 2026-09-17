# inter-cua

Goal-driven UI discovery → a reviewable capability artifact → deterministic
replay with no model in the loop → human handoff on the live session.

**Status: M0 complete.** Scaffold and the mock target app are in. The
`src/cua` packages are placeholders until M1. See the implementation plan for
the milestone breakdown; `README.md` gets its real treatment at M8.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium        # required for the browser tests
cp .env.example .env               # only `cua discover` needs an API key
```

## What runs today

```bash
make mockapp        # serves the mock legacy credit-union core on :8000
make test           # ruff + mypy --strict + pytest (44 tests, no API key needed)
python -m pytest -m "not browser"   # skip the tests that need Chromium
```

Sign on to the mock app with `operator` / `operator`. Walk
login → member 10003 → detail → sub-account → review → confirm.

## The mock target app

`mockapp/` is a deliberately hostile simulation of a legacy core banking UI,
so the perception and locator layers have something real to fight:

- the signed-in shell is a `<frameset>` (nav + main), so perception must walk
  every frame
- every layout is a nested `<table>`; no `id`, `data-*` or ARIA attributes
- no `<label for>`, so **text inputs have no accessible name** — this is what
  forces the `near_text` locator rung to exist
- buttons are `<input type=submit>`, so the accessible name is the `value`
  attribute and can change out from under a recorded capability
- form fields carry cryptic `name` attributes (`F_MBRID`, `F_DEPAMT`) because
  HTML form submission requires them. They contribute nothing to the
  accessibility tree, and nothing under `src/cua` may use them as selectors.

All data is synthetic: members 10001–10010, named `Test Member NN`. Member
10007 is flagged restricted.

### Failure injection

Arm a mode with `?inject=<mode>` on any request or the `X-Inject` header. The
mode is stored in the session so it fires on the screen it belongs to, not on
the request that armed it. `?inject=none` disarms.

| Mode | Fires at | Persistence | Exercises |
|---|---|---|---|
| `not_found` | search submit | persistent | `BusinessOutcome NOT_FOUND` |
| `validation_error` | sub-account submit | persistent | `BusinessOutcome VALIDATION_ERROR` |
| `permission_denied` | member detail | persistent | `BusinessOutcome PERMISSION_DENIED` |
| `interstitial_dialog` | after sign on | one-shot | recovery: dismiss notice |
| `slow_load` | member detail (4 s) | one-shot | recovery: bounded retry |
| `session_expired` | member detail | one-shot | recovery: re-login from last checkpoint |
| `server_error` | member detail (HTTP 500) | persistent | `Failure APP_ERROR` |
| `renamed_button` | search page ("Find") | persistent | `Failure LOCATOR_UNRESOLVED`, drift |
| `ambiguous_button` | search page (two "Search") | persistent | ladder must fall through, not guess |
| `slow_confirm` | review → confirm (6 s) | one-shot | `Failure TIMEOUT, side_effect: unknown` |

One-shot modes clear themselves the first time they fire. If they persisted,
no recovery could ever succeed and the recovery tests would prove nothing.
Persistent modes model states the app is genuinely in, so replay should report
an outcome rather than recover.

## Repo layout

```
mockapp/        the automation target
src/cua/        surface | agent | artifact | replay | policy | secrets | escalation | evidence
policies/       allowlist and risk rules (M5)
tenants/        per-deployment binding: base_url, secret refs, overlay
capabilities/   saved artifacts, git-tracked
evidence/       discovery, replay and escalation runs (M7)
tests/          unit | integration (`-m browser` needs Chromium)
```

## License

MIT.
