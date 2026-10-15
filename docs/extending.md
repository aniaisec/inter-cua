# Extending families, surfaces, providers and recoverers

Use the [own-application tutorial](your-first-application.md) for configuration
changes and [CONTRIBUTING.md](../CONTRIBUTING.md) for implementation and checks.
Extensions reuse discovery, exact-content approval and deterministic replay.

## Closed contracts

Families and named recoverers are declarative project files. Surfaces and model
providers are implemented and selected in core code; there is no arbitrary runtime
plugin loader or entry-point discovery. A class implementing a protocol alone
does not register a new target or provider.

The [capability schema](../src/cua/artifact/schema.py) closes target kinds, actions,
detector matches and recovery transitions. The [surface vocabulary](../src/cua/surface/features.py)
closes feature names and their meanings. Adding an adapter can require changes
to these core contracts, requirement inference and callers before it can be
selected. Keep unsupported features refused before application interaction.

## Add an application family

1. Initialize a `web`, `windows` or `inventory` project with `cua init PATH
   --template NAME`. Bind its tenant to a stable `app_family`, application
   location, credentials and narrowly scoped policy. Start with a read-only
   operation on a resettable target.
2. Create `capabilities/families/<app_family>.yaml` under the configured family
   directory. [FamilyTemplate](../src/cua/artifact/recorder.py) is the source of
   truth. This minimal web example declares a missing-record business answer:

   ```yaml
   target:
     vendor: MyApp
     version_hint: "1.0"
     surface: web
   outcomes:
     NOT_FOUND: {payload: {}}
   outcome_detectors:
     - code: NOT_FOUND
       class: business
       scope: {after_step: search.submit}
       match: {kind: text_present, text: No matching item}
   recovery_limits: {per_step: 1, per_run: 2}
   recoverers: {}
   redaction:
     screenshot_masks:
       - [{strategy: role_name, role: textbox, name: Password}]
   ```

   Replace the vendor, wording, control names and scope with your application's
   facts. `class: hard` produces a technical failure, `business` a declared
   business outcome, and `recoverable` requires a bounded recovery definition.
   Business codes need matching `outcomes` payload contracts. See the packaged
   [stockroom family](../src/cua/resources/templates/inventory/capabilities/families/stockroom.yaml)
   and [legacy family](../capabilities/families/legacy-core.yaml) for complete examples.
3. Run `cua --root PATH doctor --tenant local`. Missing or invalid families fail
   automatic recording preflight before model/UI work. Doctor validates the
   family file; it cannot prove application wording or future recorded step IDs.
4. Discover with a scripted tool-call sequence or an optional live provider.
   Automatic recording produces a draft. Inspect its actual step/checkpoint IDs
   before finalizing detector scopes. The recorder drops detectors/recoverers
   whose referenced steps/checkpoints are absent; a parsed family is not evidence
   that a detector made it into the capability. After changing a family, use
   `cua record RUN_DIR --families-dir FAMILY_DIR --out DRAFT.json` to record again.
   Explicit paths resolve from the invocation directory; configured roots follow
   the [project path rules](configuration.md#project-file-and-lookup).
5. Describe and review the new draft, then approve it. Check credential sinks,
   masks, outcome payloads, locator ambiguity, checkpoints, limits and every
   irreversible step. A family edit does not alter existing capability files;
   re-recording or editing content requires another exact-content review.
6. Replay a second input and a missing-record case. Assert typed outputs/business
   results against independent application state, zero replay model calls, and
   no unintended write. For writes, test input-bound consent and one observed
   commit under sequential same-key retry. Include ambiguous/renamed controls
   and an unrecoverable screen. Use the [inventory scenario](../examples/inventory/README.md)
   as an executable pattern.

Tenant overlays are currently refused. A deployment with different wording needs
a separately reviewed family/capability definition. Happy-path discovery cannot
infer all failures and recovery conditions.

## Add a named recoverer

A named recoverer usually needs only family data. After checking the recorded
IDs, append this detector to the existing `outcome_detectors` list and add
`dismiss_notice` to `recoverers`. Preserve the family's other detectors and
recoverers; choose limits for the combined behavior rather than replacing the
whole family with this fragment:

```yaml
outcome_detectors:
  - code: INTERSTITIAL
    class: recoverable
    scope: {after_checkpoint: cp.logged_in}
    match: {kind: region_present, name: System notice}
    recover: {run: dismiss_notice, then: continue, max: 1}
recovery_limits: {per_step: 1, per_run: 2}
recoverers:
  dismiss_notice:
    steps:
      - action: click
        target: [{strategy: role_name, role: button, name: OK}]
```

This permits one dismissal of the notice per step, within the run's recovery
limit. Use an exactly resolved target and a scope that cannot fire on the ordinary
login screen.
`continue` lets the interrupted step's expectations/checkpoints establish that
the screen is usable again. Inject both a dismissible notice and a persistent
notice: the latter must exhaust its cap, not loop.

A `Recoverer` has either inline `steps` (currently `click`/`press`) or a `sub_flow`
of existing capability step IDs. Relogin uses
`sub_flow: [login.username, login.password, login.submit]` with the detector's
`then: restart_from_last_checkpoint`; the engine navigates to entry and finds a
checkpoint that holds without resuming before a committed step. `retry_step`
requires `retry.allowed`; irreversible steps cannot be retried.

[RecoveryLedger](../src/cua/replay/recoverers.py) enforces detector `recover.max`,
per-step/per-run limits and the invocation budget. Execution is in
`ReplayEngine._run_recoverer` and `_restart` in [the engine](../src/cua/replay/engine.py).
Re-record, describe and approve the changed content before replay. Check recovery
evidence, exhausted limits, absent/ambiguous recovery controls, hand-back
checkpoint validation and an independent commit count. An uncertain commit needs
reconciliation; a recoverer must never press Confirm again to guess its result.

A new action or transition beyond this vocabulary needs a core schema/engine
change, requirement inference and safety regressions. Do not encode an
irreversible business operation as an inline notice-dismissal action.

## Add a surface adapter

1. Write a target proposal with an independently resettable application and an
   honest feature matrix. Implement [Surface](../src/cua/surface/protocol.py)
   and its context-manager lifecycle, following the browser or Windows adapter.
   Keep native dependencies lazy and platform guarded.
2. Produce observation-scoped refs; reject stale refs and ambiguous matches;
   preserve frame/location scopes and viewport/scale checks. Implement conditions,
   actions, settling and Surface error types. Mask any claimed screenshots before
   capture, and verify a session-handoff claim against the owned live session.
3. Declare a versioned `SurfaceDescriptor` in [adapters](../src/cua/surface/adapters.py)
   and expose the same descriptor on the implementation. Claim only working
   features. For a new feature, first add its meaning in `SurfaceFeature` and
   `FEATURE_MEANINGS`, then teach [requirement inference](../src/cua/artifact/requirements.py)
   which constructs need it. Adding a vocabulary name must not silently expand
   existing adapters' promises; review descriptors that use the full feature set.
4. Wire the target through `Target.surface` validation, tenant launch/location
   configuration, CLI discovery, `replay.runner.surface_for`, doctor and
   platform/optional dependency metadata. Catalog, workflows, HTTP and MCP must
   retain their compatibility gates. Audit web-specific requirement assumptions;
   adding a descriptor to `ADAPTERS` alone does not create an execution path.
5. Inherit [SurfaceContract](../tests/contracts/surface.py). Supply a
   `contract(check, inject='')` fixture that owns a real target, passes a
   `SurfaceCase` to the check, and verifies independent process/session cleanup in `finally`,
   even when an assertion fails. Use the
   [Chromium fixture](../tests/integration/test_surface_contract.py) and
   [UIA fixture](../tests/desktop/test_surface_contract.py) as working patterns.
   Add every new feature to `FEATURE_CONTRACTS` with a behavioral probe. Optional
   cases may skip only when the descriptor does not claim the feature.
6. Run compatibility and locator tests, the live inherited suite and one complete
   discover/review/replay scenario. Prove unsupported requests refuse before
   launch/action and old approved artifacts keep their hashes. Record platform
   execution evidence; type checking alone cannot verify an adapter.

The [CI guide](CI.md#surface-conformance) explains the common suite and manual
Windows gate. Update [platforms](platforms.md) and [support](../SUPPORT.md) only to
the extent supported by that evidence.

## Add a discovery provider

1. Implement `LLMClient` in [agent/llm.py](../src/cua/agent/llm.py): expose `provider`
   and `model`, and return a `Decision` for each `DecisionRequest`. Keep the
   SDK import inside the selected client; missing SDK/key errors must provide
   an installation/configuration remedy before browser launch.
2. Translate `ToolSpec`, `UserTurn`, `ToolResultTurn` and `Decision` without
   changing the neutral transcript. Preserve call IDs, response/model identity,
   stop reason, usage and opaque response data needed by the provider on later
   turns. Return one proposed tool action per decision; disable SDK-side tool
   execution. Deterministic policy and secret handling stay in the loop.
3. Extend the closed `Provider` literal, `PROVIDER_KEYS`, `select_client`, model
   selection and `generation_settings`. Wire explicit provider choices in CLI,
   demo and benchmark parsers and provider-specific environment/pricing/report
   paths where applicable. Add an independent optional extra, development pins
   and missing-SDK/install-matrix coverage. Keep base installs provider-free.
4. Add injected synthetic SDK responses to
   [provider tests](../tests/unit/test_llm_providers.py), with network sockets
   refused. Cover transcript round trips, tool errors, empty/no-tool decisions,
   identity/usage, model override and bounded retry behavior. Cover missing SDK
   and key precedence in [packaging tests](../tests/unit/test_packaging.py).
5. Run provider tests with the SDK extra, then base-install/import checks without
   it. Preserve `test_replay_imports_no_model_client_and_no_agent` in
   [replay tests](../tests/unit/test_replay.py). Live model checks, if used, require
   local credentials and a recorded provider/model choice; routine CI needs none.

Provider APIs and model availability vary by account and time. Document configurable
selection and failure remedies, rather than promising a particular model default.
See [provider troubleshooting](troubleshooting.md#provider-and-model-selection).

## Review and verification

Run focused checks from the [contribution matrix](../CONTRIBUTING.md#choose-checks-for-the-change).
When changing schema/feature contracts, run old golden compatibility, requirement
coverage and pre-action refusal tests. Regenerate the exported schema with
`cua schema` if its structure changes, synchronize packaged schema resources,
and explain whether a version/migration is needed. Keep prior schemas and
approval/hash semantics compatible unless an explicit migration says otherwise.

Changed artifact content resets approval and needs review. Drift repairs remain
evaluated drafts. Update docs, optional extras and the changelog alongside the
implementation, and use the [private security channel](../SECURITY.md) for a bypass.

[Documentation index](index.md) · [Project README](../README.md)
