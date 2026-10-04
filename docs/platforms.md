# Platforms and surface compatibility

Current adapters target Playwright Chromium and Windows UI Automation.
Browser automation runs where Playwright Chromium can launch, including Windows,
Linux and macOS browser environments. Native desktop automation is Windows only
and requires an interactive desktop and the `windows` extra. Additional browsers
and native macOS/Linux adapters have no support commitment in this version.

| Feature | Chromium | Windows UIA |
|---|---|---|
| Accessibility controls and typed actions | Supported | Supported |
| Frames and browser egress control | Supported | Unavailable |
| Masked screenshots, trace and vision fallback | Supported; matching needs vision extra | Unavailable |
| Attach to session for human handoff | CDP / local operator | Unavailable |
| Native dialogs | Declared dialogs only | Declared message boxes only |

Run `cua surfaces` for actual adapter descriptors and capability requirements.
Unsupported features return `SURFACE_INCOMPATIBLE` before action. The operator
console does not provide remote browser viewing; takeover uses the same local
session, with a headed browser when a person needs to see it.

## Windows setup

In the activated environment install `python -m pip install -e ".[dev,windows]"`.
Desktop discovery uses `--no-screenshots`; replay uses `--no-screenshots`
and must not request handoff or vision. Browser traces are automatically omitted
for desktop runs. See the [desktop tenant](../tenants/desk.yaml)
and [policy](../policies/deskcalc.yaml) for the reference configuration.

## Desktop reference application

`deskapp/deskcalc.ps1` is DeskCalc: a WinForms window, run by Windows
PowerShell, with no clock and no randomness. It has two number fields named
only by the label beside them, an Operation combo box, a Round to cents
checkbox, Calculate, a read-only Result, and Record. Record is the commit: it
appends a line to the ledger file (`DESKCALC_LEDGER`, default
`%TEMP%\deskcalc-ledger.txt`), which the tests read to judge what happened.
`-Inject` switches on one fault per launch, as `?inject=` does on the entry
location:

| Mode | Effect |
|---|---|
| `renamed_button` | Calculate is labelled Compute |
| `ambiguous` | a second Calculate button, in a Legacy group |
| `disabled` | Calculate is disabled |
| `slow` | the result appears 2.5 s after Calculate |
| `modal` | Calculate raises a native message box instead of a result |

Dividing by zero shows "Cannot divide by zero" in a label, which the
`deskcalc` app family (`capabilities/families/deskcalc.yaml`) turns into the
business outcome `DIVIDE_BY_ZERO`.

See [surface invariants](architecture.md#surface) and [test coverage](testing.md).

[Documentation index](index.md) · [Project README](../README.md)
