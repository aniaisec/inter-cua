# Extending families, adapters and providers

Start with [your first application](your-first-application.md). Extensions should
reuse discovery, approval and replay rather than introduce routes around their gates.

## Families and recoverers

A family lives at `capabilities/families/<app_family>.yaml`; its schema is
`FamilyTemplate` in [the recorder](../src/cua/artifact/recorder.py). It contributes
target, outcome payloads, scoped detectors, recovery limits, recoverers and redaction.
The recorder includes only step/checkpoint references the capability actually has.
Inspect recorded IDs before selecting scopes, and verify the final artifact contains
the intended detectors. [Legacy core](../capabilities/families/legacy-core.yaml) and
[DeskCalc](../capabilities/families/deskcalc.yaml) are separate reference families.
Tenant overlays are currently refused. A deployment with different wording needs
a separately reviewed family/capability definition.

Executors live in [replay recoverers](../src/cua/replay/recoverers.py). Use bounded
per-step/per-run recovery and explicit expectations. Irreversible steps cannot be
retried; uncertain commits need reconciliation, not a recoverer pressing Confirm
again. Test business outcome detection separately from technical failure and recovery.

## Surface adapters

Implement [Surface](../src/cua/surface/protocol.py), publish an honest
[feature descriptor](../src/cua/surface/features.py), and register in
[adapters](../src/cua/surface/adapters.py). Provide observation-scoped references,
exactly-one-node resolution, condition evaluation, safe geometry, dialog behavior
and lifecycle cleanup. Claim optional features only when they work; requirement
gates run before actions. Use browser/Windows tests as examples and real platform
verification rather than treating type checking as UI evidence.

## Discovery providers

The abstraction is in [agent/llm.py](../src/cua/agent/llm.py), with provider translation
and scripted-client tests in [the suite](testing.md). Translate the tool vocabulary,
preserve response identity/usage evidence, and keep policy enforcement outside the
model. Paid credentials belong in local secrets and optional live checks. Replay
must import no provider client or agent.

## Review and verification

Changed artifact content resets approval and needs exact-content review. Drift
repairs are evaluated drafts, not automatic deployments. Run focused invariant tests
and [quality/browser gates](CI.md); verify desktop changes in an interactive session.
See [architecture](architecture.md) and [security policy](../SECURITY.md).

[Documentation index](index.md) · [Project README](../README.md)
