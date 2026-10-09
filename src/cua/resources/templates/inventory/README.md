# Your first application: inventory

This project starts without any capabilities, approvals or review receipts.
The scripts use the ordinary discovery loop without a provider key. Discovery
of the write really changes synthetic stock; use only the loopback demo target.
Restart that target to reset stock, receipts and sessions.

## Start the target and configure the project

Install inter-cua and Chromium first (`python -m playwright install chromium`).
From an installed distribution, start the target in a separate terminal:

```sh
python -m uvicorn inventoryapp.app:app --host 127.0.0.1 --port 8001
```

From a source checkout instead, run at its root:

```sh
python -m uvicorn examples.inventory.app.app:app --host 127.0.0.1 --port 8001
```

Create this project with `cua init inventory-project --template inventory`.
Enter the generated directory. PowerShell:

```powershell
Set-Location inventory-project
Copy-Item .env.example .env
```

POSIX:

```sh
cd inventory-project
cp .env.example .env
```

The commands below each occupy one line and work in both shells. Edit
`tenants/local.yaml` if you choose a different port; then check readiness:

```sh
cua doctor --tenant local --json --probe-browser
```

The demo begins with SKU-001 (Canvas tote, 12 units) and SKU-002 (Desk lamp,
4 units). Its only login is the synthetic `clerk:practice-password` in `.env`.
The target must bind to loopback: it has no production authentication or storage.

## Discover, review and replay a lookup

```sh
cua discover --tenant local --name item_lookup --entry /login --goal "Look up an item and return its available quantity and name" --param item_code:string=SKU-001 --output quantity:integer --output item_name:string --llm scripted --script scripts/discovery/item_lookup.yaml
cua describe item_lookup
cua approve item_lookup --by local-reviewer
cua replay item_lookup --input item_code=SKU-001
cua replay item_lookup --input item_code=SKU-002
cua replay item_lookup --input item_code=SKU-999
```

Discovery produces `capabilities/item_lookup.json` as a **draft** and evidence
under `evidence/runs/run_*/`. Inspect both before approving. The first two
replays exit 0 with `kind: success`; the second returns integer `quantity: 4`
and `item_name: Desk lamp`. The missing item exits 2 with
`kind: business_outcome`, `code: NOT_FOUND`, and `side_effect: none`.
Replay evidence contains no `model_calls.jsonl`; discovery has scripted call
records. Approval seals exactly the content you described; any edit needs a new
description and approval.

## Discover and consent to a stock adjustment

The next discovery applies **-2 units** to SKU-001, leaving 10. The flag grants
risky-action permission during this synthetic discovery only. It creates neither
an approved artifact nor consent for a future replay. For your own application's
writes, use a resettable test deployment and the operator approval path.

```sh
cua discover --tenant local --name adjust_stock --entry /login --goal "Adjust stock for an item and return the receipt and new available quantity" --param item_code:string=SKU-001 --param quantity_change:integer=-2 --output receipt:string --output quantity:integer --llm scripted --script scripts/discovery/adjust_stock.yaml --auto-approve-risky
cua describe adjust_stock
cua approve adjust_stock --by local-reviewer
cua replay adjust_stock --input item_code=SKU-001 --input quantity_change=-3
```

The last command stops with `NEEDS_APPROVAL` and makes no adjustment. Capability
approval permits the procedure; an invocation token consents to the exact item,
change, capability version, and tenant. Mint one for -3 and keep one retry key.
PowerShell:

```powershell
$consent = cua approval-token adjust_stock --input item_code=SKU-001 --input quantity_change=-3 --by local-reviewer
cua replay adjust_stock --input item_code=SKU-001 --input quantity_change=-3 --approval-token $consent --idempotency-key inventory-adjust-001
cua replay adjust_stock --input item_code=SKU-001 --input quantity_change=-3 --approval-token $consent --idempotency-key inventory-adjust-001
```

POSIX:

```sh
consent=$(cua approval-token adjust_stock --input item_code=SKU-001 --input quantity_change=-3 --by local-reviewer)
cua replay adjust_stock --input item_code=SKU-001 --input quantity_change=-3 --approval-token "$consent" --idempotency-key inventory-adjust-001
cua replay adjust_stock --input item_code=SKU-001 --input quantity_change=-3 --approval-token "$consent" --idempotency-key inventory-adjust-001
```

The first run returns `side_effect: committed`, receipt `ADJ-0002` and integer
quantity 7. The retry returns `cached: true`, the same receipt, and makes no UI
action. Independently inspect the read-only oracle in a browser at
`http://127.0.0.1:8001/_test/state`: quantity 7, commit_count 2 (discovery plus
replay), commit_posts 2. The automation policy excludes `/_test/state`.
Retry with the same key after a lost response. An `unknown` side effect requires
checking the target before any new write. This example demonstrates sequential
retries; concurrent/crash-safe invocation ownership is subsequent runtime work.

These commands require no token because their business outcomes occur before
the consent-requiring commit:

```sh
cua replay adjust_stock --input item_code=SKU-001 --input quantity_change=-100
cua replay adjust_stock --input item_code=SKU-001 --input quantity_change=0
cua replay adjust_stock --input item_code=SKU-001 --input quantity_change=oops
```

Insufficient stock and zero change exit 2 with `INSUFFICIENT_STOCK` and
`VALIDATION_ERROR`. The noninteger input exits 1 with `INPUT_INVALID` before
starting the target session. All three make no stock adjustment.

## Exercise recovery and refusal

```sh
cua replay item_lookup --input item_code=SKU-002 --inject session_expired
cua replay adjust_stock --input item_code=SKU-001 --input quantity_change=-1 --inject validation_fault
cua replay item_lookup --input item_code=SKU-002 --inject renamed_control
cua replay item_lookup --input item_code=SKU-002 --inject ambiguous_control
cua replay item_lookup --input item_code=SKU-002 --inject changed_screen
```

Session expiry uses the family relogin subflow, then resumes after `cp.logged_in`.
Validation fault returns `VALIDATION_ERROR` before commit. Renamed and duplicated
buttons refuse unresolved/ambiguous locators; if only a coordinate rung resolves,
unattended replay refuses that pixels-only target too. Changed screen reaches the right
URL but fails its `Item details` checkpoint. Inspect code, step, screenshot and
side effect in the JSON and evidence; do not treat every nonzero exit as a retry.

To recover the changed screen, start `cua operator --tenant local` in another
terminal in this project. Then:

```sh
cua replay item_lookup --input item_code=SKU-002 --inject changed_screen --handoff --headed --handoff-wait 0
```

It exits 3 with a resume token. Open the operator console at
`http://127.0.0.1:8100`, take control, click **Show item details** in the shared
browser, and hand back. The automation remains paused while the person acts.
Run `cua resume <resume_token>` in this project. Hand-back validates checkpoints
and extracts quantity 4 without repeating the lookup. A person can likewise
press **Find item** for the renamed-control case. Only the local headed browser
is viewable; remote browser viewing is not implemented.

## Replace the sample with your application

`cua.toml` selects version 1 project behavior, default tenant and project-relative
directories. `tenants/local.yaml` binds `id`, UI `app_family`, application
`base_url`, policy path and secret sources. Change the URL and family together
with your own family file. `inventory/clerk` reads the environment variable and
splits `username:password`; the capability keeps a tenant-relative secret
reference and credential placeholders. Omit login steps/bindings for an
unauthenticated app. `.cua/approval-signing.key` is unique to this project and
permits signing invocation consent; keep it private and out of Git.

`policies/inventory.yaml` permits only the tenant origin, exact application
paths and listed actions. `credential_paths` restricts secret typing to login.
`sensitive_labels` and `screenshot_masks` hide the password in observations and
pictures. External navigation and downloads are blocked; vision is off.
`risky_rules` identifies **Apply adjustment** on `/review/` as the stored write.
Audit all ways your app can commit, including keyboard/form submission. A
renamed write must remain covered by a reviewed risk rule; do not weaken the
policy merely to get discovery through.

`capabilities/families/stockroom.yaml` supplies vendor/version/surface metadata
and facts discovery cannot learn from one success. `outcomes` declares business
answers and payload shapes. Each detector gives a `code`, `class`, tested `match`
condition and `scope`: lookup NOT_FOUND applies after `lookup.submit`; shortage
applies after preparation or commit; validation applies after `adjust.submit`.
AUTH_FAILED is a hard failure after login. SESSION_EXPIRED is recoverable only
after `cp.logged_in`, uses the named `relogin` subflow, and restarts from the
last checkpoint. `recovery_limits` bounds attempts; `redaction` adds family
masks. The recorder retains only rules whose step/checkpoint/subflow exists.
Review the generated IDs before trusting scope. A missing family fails before
model work or UI launch; `--no-record` deliberately skips recording preflight.

Replace the scripts' labels, entry path, sample literals and output table
locators. Declare every variable input with `--param`; a matching typed literal
becomes `${input_name}`. Types are string, integer or decimal; the recorder does
not infer application ranges from one example. Add reviewed input patterns when
appropriate, then describe/approve again. Output types and extraction locators
must match your app; prefer labels, roles and table headers to coordinates.
Alternatively omit `--script` and choose `--llm gemini` or `--llm anthropic` with
the corresponding provider extra and key. Live discovery is optional, incurs
provider charges and is excluded from routine CI.

Before approval, inspect every locator rung for uniqueness, credential sinks,
checkpoints for both page identity and bound item identity, output extraction,
detector coverage and retries. **Apply adjustment** must be irreversible,
approval-required, nonretryable, with an **Adjustment saved** side-effect marker.
Test another input, not-found, validation, shortage, denial, session expiry and
changed/ambiguous controls against an independent oracle. One happy-path
discovery cannot infer all business failures or recovery conditions.

Stop the target and console with Ctrl+C. Delete this project only when its
evidence is no longer needed. Restart the target and initialize a new project
to repeat the walkthrough; existing files are never overwritten by init.
