# Getting started

Follow the separate [PowerShell and POSIX first-run blocks](../README.md#installation-and-no-key-demo)
in the README. Those are the canonical installation and short-demo commands.
Run them from a checkout: the demo currently copies repository resources and is
not an installed-wheel demonstration. Python 3.11 or newer and a Playwright Chromium
installation are required. The runner starts its own services on free ports.

## What to expect

The default scripted provider needs no API key. Each run creates a fresh
`demo/demo_<session-id>/` directory. All seven stages must pass before `summary.md`
and `summary.json` are generated: discovery, exact-content approval, independent
replay, drift, takeover/resume, single-use consent, and a hostile-page scenario.
With `--repetitions 3`, expect three successful lookup replays, zero replay model
calls, one commit and one blocked security attack. Review `manifest.json` if the
runner fails. Its `workspace/runs/` directories hold the actual evidence.

The reviewer and operator are scripted in this synthetic demo. Their activity
shows the mechanism; it does not measure human effort or live model planning.
For the complete 100-replay run, optional live discovery, output layout and report
regeneration, see the [demo guide](../scripts/demo/README.md).

For manual discovery, approval and interactive takeover, follow the
[review and replay walkthrough](review-and-replay.md).

## Explore the application yourself

Activate the environment and run `cua mockapp` in one terminal. It listens on
`http://127.0.0.1:8000` and accepts synthetic login `operator` / `operator`.
In another activated terminal, set the synthetic credential and replay:

PowerShell:

```powershell
$env:CUA_SECRET_MOCKCORE_OPERATOR = 'operator:operator'
cua replay capabilities/member_savings_balance.json --input member_id=10003
cua replay capabilities/member_savings_balance.json --input member_id=99999
```

POSIX:

```sh
export CUA_SECRET_MOCKCORE_OPERATOR='operator:operator'
cua replay capabilities/member_savings_balance.json --input member_id=10003
cua replay capabilities/member_savings_balance.json --input member_id=99999
```

The first exits 0 with balance `1411.21`; the second exits 2 with `NOT_FOUND`.
Stop the mock app with Ctrl+C when finished. See [CLI results](cli.md#results-and-exit-codes),
[configuration](configuration.md) and [troubleshooting](troubleshooting.md).

[Documentation index](index.md) · [Project README](../README.md)
