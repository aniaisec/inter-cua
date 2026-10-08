# Your first application

Complete the [no-key demo](getting-started.md) first. Create your own project
directory and a `cua.toml` containing `version = 1`, then create the files below
inside it. Alternatively, run `cua init my-project --template web` and edit its
starter files and generated guide. Commands find that project from nested
directories; use `cua --root /absolute/project/path <command>` from elsewhere.
See [project configuration](configuration.md) for
custom directories and explicit CLI path behavior. Start with a read-only
operation on a test deployment you can inspect and reset.

## 1. Bind a tenant

Create `tenants/my-app.yaml`, replacing the example URL and secret variable:

```yaml
id: my-app
app_family: my-product
base_url: https://test.example.invalid
policy: policies/my-app.yaml
secrets:
  login:
    provider: env
    var: MY_APP_LOGIN
    format: "username:password"
  cua/approval-signing-key:
    provider: file
    path: .cua/my-app-signing.key
```

Set `MY_APP_LOGIN` in the environment or local `.env`. Use this tenant's own secret
sources and key. If the application needs no login, omit the login binding.
An app family names a product's UI contract, not one tenant's URL.

## 2. Define the policy

Create `policies/my-app.yaml` by adapting the [reference policy](../policies/default.yaml).
Replace banking paths with narrowly scoped paths for your application's sign-on,
lookup and result screens. Keep origins tied to `{tenant.base_url}`. Set credential
paths to actual sign-on screens; choose sensitive labels, screenshot masks and scrub
patterns for this application. Declare risky controls and keep external navigation
and downloads blocked. Do not retain banking path regexes blindly.

## 3. Create the family before discovery

Create `capabilities/families/my-product.yaml`. A schema-valid starting point is:

```yaml
target:
  vendor: MyProduct
  version_hint: test-deployment
  surface: web
outcomes: {}
outcome_detectors: []
recoverers: {}
```

This skeleton declares no business outcomes or recovery. Before using a capability,
add real not-found, validation, permission and session-expiry detectors with
appropriate step scopes. Follow [family and recoverer guidance](extending.md).
One happy-path discovery cannot infer failure screens. Use a unique family;
legacy-core's banking wording and checkpoint IDs do not fit arbitrary apps.
Automatic recording currently looks for this file after discovery; a missing
family can waste a successful run. Create it first.

## 4. Discover a read-only operation

From an activated environment at the root, substitute your goal, entry path and
input/output names. This is a template, not a runnable sample target; the one-line
command works in PowerShell and POSIX shells:

```sh
cua discover --tenant my-app --name item_lookup --entry /login --goal "Look up an item by its code and return the displayed quantity" --param item_code:string=SAMPLE-001 --output quantity:integer --capabilities-dir demo/my-app --llm gemini
```

Live discovery needs the selected provider's key and incurs charges. Scripted
discovery needs an application-specific tool-call script; the banking script cannot
discover a different application. Use `--no-record` deliberately only for discovery
evidence without a draft. For Windows targets, follow [platform constraints](platforms.md)
and the desktop reference family.

## 5. Review, approve and replay

Inspect discovery evidence and the draft. Review credential sinks, every locator,
checkpoints, declared outcomes, retry rules, side effects and output extraction.
Then run from the same root in either shell:

```sh
cua describe demo/my-app/item_lookup.json
cua approve demo/my-app/item_lookup.json --by reviewer
cua replay demo/my-app/item_lookup.json --tenant my-app --input item_code=SAMPLE-001
```

Approval binds to exact content most recently described; edits need another review.
Artifact approval and invocation consent for a write are distinct. Before approving
a write, verify its irreversible step, consent requirement and commit marker.
Use [consent and idempotency](cli.md), and independently check application state.

## 6. Test another input and a failure

Check a second item, a missing item, a denied action and an expired session against
an independent oracle. Change or duplicate a control to verify safe refusal. Inspect
`result.json`, masked evidence and side-effect status. Re-record or propose a reviewed
drift candidate when the UI changes. One working sample does not establish coverage
of every business condition.

When reliable, select [CLI, HTTP or MCP](integrations.md), or compose approved
operations in a [workflow](architecture.md#workflows).

[Documentation index](index.md) · [Project README](../README.md)
