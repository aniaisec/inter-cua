# Single-host operator deployment

The supported topology is one runtime and operator console per runs directory on
one host, on trusted local storage. PR16 adds operator authentication and durable
assignment. PR13 workflow/service envelope migration and PR15 verified browser
termination/profile cleanup remain incomplete; PR17's shared-service recovery
release gate is not yet satisfied. Do not run distributed workers or advertise
exactly-once UI effects.

## Configure an operator

Operator accounts are independent of API clients. An API bearer key grants no
console access. Copy [the operator example](../operator/access.example.yaml) to
`operator/access.yaml`, remove unneeded accounts, and grant explicit tenant roles:

```yaml
version: 1
operators:
  ann:
    key: {provider: file, path: .cua/operator-keys/ann.key}
    tenants:
      local: [read, operate, approve]
```

Generate a unique secret of at least 32 characters for each account. This command
works in PowerShell and POSIX from the selected project root, refuses an existing
key, and keeps the secret out of command output:

```text
python -c "from pathlib import Path; import secrets; p=Path('.cua/operator-keys/ann.key'); p.parent.mkdir(parents=True, exist_ok=True); p.open('x', encoding='utf-8').write(secrets.token_urlsafe(32))"
cua operator --tenant local --operator-access operator/access.yaml
```

Restrict the key file/directory to the runtime service account and the person
managing credentials (Windows ACLs or POSIX directory mode 0700/file mode 0600).
For an environment binding use `key: {provider: env, var: CUA_OPERATOR_ANN_KEY}`
and supply it through the supervisor's private secret mount/environment. Do not
place keys in URLs, command arguments, source control or shared logs. Key-file
references are project-relative, even when the access file lives elsewhere.
Missing, short or shared credentials fail startup; there is no anonymous fallback.

Open the console and use the browser's authentication prompt: username `ann`,
password the private key's contents. `read` can inspect this tenant's requests,
masked screenshots and transition history. `operate` can claim, resume, retry or
abort. `approve` additionally allows approval of an offered consent action and
also requires `operate`. Every tenant grant includes `read`. Operators see only
the console's selected tenant; that tenant's policy scrubs captured browser data.
Explicit operator grants can cover requests from that tenant's API clients, but
do not confer API invocation rights or approve capability artifacts.

The application uses HTTP Basic, with no application session cookie. Browsers
cache credentials; use a dedicated browser profile, close it when finished and
do not use shared unattended workstations. Form CSRF tokens bind identity,
tenant, request and action, expire after 15 minutes, and become invalid on restart.
Reload an old form. JSON actions require Basic authentication and
`X-Cua-Operator-Request: 1`; bodies contain only `resume_at` and/or `why`, never
`by`. Cross-origin writes are refused. No permissive CORS configuration is supplied.

Assignment is persisted with the control state and audit transition. A competing
operator cannot take the same intervention, approve it, abort it or hand it back.
The winner's verified account name is used throughout. An administrator rotating
credentials should retain that stable identity to recover an assigned request;
there is no reassignment or lease-stealing endpoint. Existing human-control
records without an assignee are refused for operator mutation and require
reconciliation; they are not silently assigned to the next caller. Resume still
validates the application's checkpoint. A login is not proof that a commit was
absent or that a checkpoint holds.

## TLS and private browser access

Bind both services to loopback. For access from another machine, expose only the
operator HTTP listener through a supervised TLS reverse proxy. HTTP Basic over
plaintext beyond loopback is unsupported. Terminate TLS at that trusted local
proxy; forward the original Host and protocol, preserve Authorization, and trust
forwarded headers only from that proxy. Match allowed hostnames at the proxy.
For example, the relevant Nginx location inside a configured TLS virtual host is:

```nginx
location / {
    proxy_pass http://127.0.0.1:8100;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $remote_addr;
}
```

Provision/renew certificates through your existing deployment tooling; restrict
network access to intended operators. Never log Authorization or secret-bearing
request bodies at the proxy. Keep the service's default no-store responses and
framing protections. Readiness requests need their own read-only operator key;
do not weaken authentication for a load balancer. TLS/proxy deployment is a
configuration example, not a validated container distribution.

Chromium DevTools endpoints remain private loopback endpoints on the same host.
Do not reverse-proxy, port-forward or publish them. Normal request JSON and pages
omit CDP addresses. Only the assigned operator with `operate` can fetch
`GET /api/requests/{id}/session` for trusted local tooling. For interactive work,
use `--handoff --headed` in the host's session. Authenticated remote viewing of
the browser is not implemented. Captured screenshots remain masked evidence,
not a live remote desktop.

## Supervision, rotation and recovery

Run as a dedicated non-administrator account. Give only that account write access
to the SQLite authority, runs, registry and consent records; give the operator
process the same consistent paths and secret mounts. Configure absolute project
roots in the supervisor; avoid a supervisor that launches duplicate runtimes.
Install Chromium for that service account and set a writable private temporary
directory. Configure worker/queue bounds and filesystem capacity/alerts using
[operations](operations.md#bounded-admission-and-shutdown) and the scoped metrics.
Retention removes eligible screenshots only and is not a disk-space guarantee.

The operator's authenticated `/health` reports HTTP liveness. Its `/ready` returns
503 if the runs directory is unavailable or queue/control/invocation authority
cannot be read consistently. Readiness does not launch a browser, call a model,
prove secret availability in the target app, or guarantee a UI commit outcome.
GETs do not expire or finalize handoffs. Waiting replay workers expire their own
requests; orphaned expired requests remain visible and cannot be claimed or
approved. Inspect/reconcile their evidence before resuming operation.

Rotate keys by stopping console admission, changing the mounted credential,
then restarting the console with the same operator identity. In-memory keys are
loaded at startup. Old credentials stop authenticating after restart; restart also
invalidates old form tokens. Preserve the durable assignment/history. Rotation
does not revoke someone's physical access to an already-open browser.

Before upgrading/backing up, stop API/MCP admission, drain active work at safe
boundaries and stop all CLI/operator writers. Preserve the complete snapshot in
[backup/restore instructions](operations.md#maintenance-pr15-first-increment),
including registry, invocation authority, evidence, spent consent and mounted
secrets. Do not restore an older authority over newer retry tombstones.

Exercise recovery on isolated synthetic data before service use:

1. Start bounded runtime and authenticated console. Confirm anonymous reads and
   actions fail, and a reader cannot approve.
2. Escalate a run, claim it as one operator and confirm another gets a conflict.
   Restart the console; verify the assignment and audit history remain.
3. Hand back at a known checkpoint; verify the worker validates it and records
   the original operator. A wrong checkpoint must refuse rather than skip work.
4. Interrupt a worker around a synthetic write. Retry the original identity and
   confirm the durable answer or explicit uncertainty, with no automatic repeat.
5. Stop all writers, back up and restore into an isolated directory, run doctor
   and readiness, then inspect outcomes before enabling one runtime.
6. Run scoped maintenance in dry-run mode. Confirm handoffs, unknown outcomes and
   retry/approval tombstones survive, then apply only eligible retention.

The automated authentication, assignment and handoff tests cover code behavior.
This supervisor/TLS/restore exercise remains an operator release procedure; it
does not claim an end-to-end production recovery trial has been completed.

Browser-only hosting requires Chromium and the service user's permissions.
Windows UI Automation additionally requires an unlocked interactive Windows
session and the optional Windows dependencies. A Windows service session, headless
container or Linux browser test does not validate UIA. See [platforms](platforms.md).

[Documentation index](index.md) · [Operations](operations.md)
