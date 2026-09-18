# inter-cua

Goal-driven UI discovery → a reviewable capability artifact → deterministic
replay with no model in the loop → human handoff on the live session.

**Status: M1 complete.** Scaffold, the mock target app, and the Surface
layer — perception, the locator ladder and the condition vocabulary — are in.
The remaining `src/cua` packages are placeholders, each landing in a later
milestone; `cua --help` names the milestone for every subcommand that is not
wired up yet. `README.md` gets its real treatment at M8.

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
make test           # ruff + mypy --strict + pytest (131 tests, no API key needed)
python -m pytest -m "not browser"   # skip the tests that need Chromium

# What the automation sees, for a human. Needs `make mockapp` running.
python -m cua.surface --url http://127.0.0.1:8000/login
python -m cua.surface --url http://127.0.0.1:8000/login --signed-in
```

Sign on to the mock app with `operator` / `operator`. Walk
login → member 10003 → detail → sub-account → review → confirm.

`python -m cua.surface --signed-in` signs on through the same locator ladders a
recorded capability will use, and prints the compact tree for the screen it
lands on:

```
# MockCore 1.0 :: http://127.0.0.1:8000/
[nav] http://127.0.0.1:8000/nav
  n52 text "Functions"
  row: n57 link "Member Search"
  row: n60 link "Account Inquiry"
  row: n63 link "Reports"
  row: n66 link "Sign Off"
[main] http://127.0.0.1:8000/search
  n67 text "MockCore Member Services 1.0"
  n68 heading "Member Search"
  row: n72 cell "Member ID" | n74 textbox ~'Member ID' | n76 button "Search"
  n77 paragraph "Enter a five digit member number."
```

The refs start at `n52` because signing on took four looks at the screen first,
and no observation reuses another's numbers.

The `~'Member ID'` is the label the textbox does not have: the app gives it no
accessible name, so perception records the text beside it, and the `near_text`
locator rung finds it by the same geometry.

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
| `modal_dialog` | member detail (in-page overlay) | one-shot | recovery: dismiss the overlay; a click under it faults instead of going nowhere |
| `native_confirm` | review → confirm (`window.confirm`) | persistent | undeclared: dismissed and reported, nothing committed → escalate; declared: answered as the capability says |

One-shot modes clear themselves the first time they fire. If they persisted,
no recovery could ever succeed and the recovery tests would prove nothing.
Persistent modes model states the app is genuinely in, so replay should report
an outcome rather than recover.

The two dialog modes are different problems. An in-page overlay is ordinary
markup, so perception sees it; it just covers the screen. A native
`confirm()` never enters the accessibility tree or a screenshot, and while it
is open the browser accepts no further instruction — so the surface answers it
the moment it appears (as the capability declared, otherwise by dismissing,
which commits nothing) and records the answer on the action result and the
next observation. Playwright's default is to dismiss silently, which would make
a cancelled irreversible step look like a successful one.

## Repo layout

```
mockapp/        the automation target
src/cua/surface/  protocol | a11y | locators | conditions | playwright_surface
src/cua/        agent | artifact | replay | policy | secrets | escalation | evidence
policies/       allowlist and risk rules (M5)
tenants/        per-deployment binding: base_url, secret refs, overlay
capabilities/   saved artifacts, git-tracked
evidence/       discovery, replay and escalation runs (M7)
tests/          unit | integration (`-m browser` needs Chromium)
```

## The Surface layer

Everything that touches the target goes through `Surface`, so the rest of the
system never learns that this particular target is a web page.

- **Perception** takes one `aria_snapshot()` per frame (a `<frameset>` has no
  single tree, and `page.accessibility.snapshot()` is deprecated) and flattens
  the result into refs, roles, names, values, boxes and parents. Refs are
  scoped to one observation and never reused, so a ref that outlives its screen
  addresses nothing instead of addressing whatever took its place.
- **The locator ladder** is how a step names a control: `role_name`, then
  `near_text`, then `table_cell`, then `bbox`. Every rung must resolve to
  exactly one node; a rung that matches nothing *or matches several* falls
  through to the next, and the run records which rung answered. Pixels are
  refused outside the viewport the capability was recorded in.
- **Conditions** (`location_matches`, `region_present`, `text_present`,
  `value_set`, `error_banner_present`, `validation_message_present`, ...) are
  pure functions of an observation, so the same check that a step waited for
  can be re-run later against recorded evidence. Each one documents what it
  would mean on a desktop target.

No CSS or XPath selector appears anywhere under `src/cua`. The only exception
is the document-level `body` anchor that `aria_snapshot` requires, which never
names a control.

## License

MIT.
