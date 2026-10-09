# CLI, HTTP and MCP integrations

Choose CLI for a shell or subprocess caller, HTTP for a shared local service,
and MCP for a client that launches a stdio tool server. All use approved artifacts
and the same replay gates. [Workflows](architecture.md#workflows) compose capabilities.

The [runnable examples](../examples/README.md) exercise these contracts through
isolated, no-key scenarios. Each example's documented command and expected JSON
are checked against its `scenario.json`. Start with [typed CLI lookup](../examples/lookup/README.md),
the [HTTP client](../examples/http_client/README.md), or [MCP setup](../examples/mcp_setup/README.md).

## CLI callers

`cua catalog` is the surface an agent sees. It is a view of the capability
registry: one tool per capability, the highest approved version, because only
an approved version runs unattended. Deprecated and revoked versions are never
offered.
`--json` prints each one as a tool definition (`name`, `description`,
`input_schema`), the shape a model's tool-calling API takes. The description
carries the contract: outputs, side effect, whether it is idempotent, and
which business outcomes and escalations to expect.

```bash
cua catalog
cua catalog --json
cua catalog invoke member_savings_balance --args '{"member_id": "10003"}'     # 0 success
cua catalog invoke member_savings_balance --args '{"member_id": 10003}'       # 1 INPUT_INVALID: member_id is a string
```

Arguments are typed the way a model types a tool call. A `decimal` may come as
a number or a string, but a `string` must be a JSON string: a member id sent
as a number would lose any leading zero. A wrong type fails before a browser
starts. `--request <file>` (or `-` for stdin) takes a whole invocation request
(`capability`, `inputs`, `idempotency_key`, `approval`, `budget`) as JSON.

An invocation that needs a person returns `escalated` and exits, leaving the
browser up. The caller gets consent from someone who may give it and carries
the run on:

```bash
cua catalog invoke open_subaccount --input member_id=10003 --input initial_deposit=250.00 --handoff --handoff-wait 0
```

(exit 3: `escalated`, `NEEDS_APPROVAL` at `review.submit`, `side_effect: none`, and a `resume_token`)

```bash
cua resume <resume_token> --approval-token "$(cua approval-token capabilities/open_subaccount.json --input member_id=10003 --input initial_deposit=250.00 --by reviewer)"
```

(exit 0: `success`, `side_effect: committed`, the reference number. The resumed
run found the review screen still holding, so it carried on at
`review.submit` with the fresh consent. A token for other inputs is refused and
the run keeps waiting.) The same request also shows up on `cua operator`, where
**Approve action** does the same thing.

## HTTP

`cua serve` puts the same runtime behind HTTP, for an agent or a service that
should not start a CLI process per call. It binds to `127.0.0.1:8200`; the
OpenAPI page is at `/docs`.

```bash
cua mockapp                  # in one terminal
cua serve                    # in another; creates .cua/api-keys/local-agent.key on first start
```

[`api/access.yaml`](../api/access.yaml) says who may call it. Every request but
`GET /health` carries three things:

- `Authorization: Bearer <key>`: which client is calling. Keys are bound like
  tenant secrets, to a file or an environment variable, never written in the
  access file.
- `X-Cua-Tenant: <id>`: the tenant the call is for. It is never inferred.
- `X-Request-Id: <id>`, required on every POST and echoed on every answer.
  It ties the caller, the server's request log (`<runs>/.api/requests.jsonl`)
  and the run record together.

A client names its tenants, the capabilities it may see and run, and its
scopes: `read`, `invoke`, `approve` (carry a signed approval token into a
run) and `operate` (resume and abort escalated runs).

| Endpoint | What it does |
|---|---|
| `GET /health` | up or not; no key needed |
| `GET /capabilities` \| `/capabilities/{name}` \| `/capabilities/{name}/versions` | what `cua catalog` shows, for the tenant's application family and the client's capabilities: the tool definition, the contract, the lifecycle. No steps, locators or credential references |
| `POST /runs` | start a run: `{"capability", "version"?, "inputs", "approval"?, "budget"?, "handoff"?}`, typed like `catalog invoke --args` |
| `GET /runs/{id}` | the run, and its `ReplayResult` once there is one |
| `GET /runs/{id}/events` | its canonical events (`cua metrics run --events`) |
| `POST /runs/{id}/approve` | `{"token", "approved_by"?}`: consent for the commit an escalated run stopped at |
| `POST /runs/{id}/resume` | hand an escalated run back to the automation, as `cua resume` does |
| `POST /runs/{id}/abort` | end an escalated run as `ESCALATION_ABORTED`, as the console's Abort does |

A run gets its id at once and executes on a worker. The POST answers `202`
while the run executes, or waits for it with `?wait=<seconds>` (up to 300).
The run's `result` is the `ReplayResult` that `cua replay` prints: the same
fields, in the same order. It is `null` until the run stands still in one of
these states:

- `escalated`: waiting on a person or on consent;
- `finished`: success, business outcome or failure;
- `error`: the request could not be made at all, as `cua replay` exits 64;
- `lost`: the server stopped mid-run and the run directory holds no result.

`running` currently also includes work waiting for a worker. The reusable
[HTTP client](../examples/http_client/client.py) accepts future `queued` responses
as active states; the server will distinguish them in PR14. It stops polling at
`escalated`, `finished`, `error` or `lost`, leaving the caller to inspect the result
and decide. A `finished` business outcome is an answer, not a transport error.
`lost`, or a result with `side_effect: unknown`, requires reconciliation and must
never trigger an automatic replacement write. An unknown response state fails closed.

```bash
KEY=$(cat .cua/api-keys/local-agent.key)
curl -s -X POST "http://127.0.0.1:8200/runs?wait=60" \
  -H "Authorization: Bearer $KEY" -H "X-Cua-Tenant: local" -H "X-Request-Id: agent-req-1" \
  -H "Content-Type: application/json" \
  -d '{"capability": "member_savings_balance", "inputs": {"member_id": "10003"}}'
```

A capability that is not read-only needs an `Idempotency-Key` header, and is
refused with `428` without one. The same key and request return the run it
first started (`Idempotent-Replayed: true`), including while that run is still
executing. The same key with other inputs is `409`. Keys are scoped to the
client and tenant: the runner caches results under `api:<tenant>:<client>:<key>`,
and the caller gets its own key back. A commit follows the CLI's path over
HTTP:

1. `POST /runs` with `"handoff": {}` comes back `escalated` (`NEEDS_APPROVAL`)
   with the browser left up.
2. `POST /runs/{id}/approve` with a token from `cua approval-token` carries it
   on. A token for other inputs is `403`, and the run keeps waiting.

Approving over HTTP takes a signed token, never a name: there is no person at
a console to vouch for who is consenting.

Persist the key and exact request before sending. A transport retry must carry
the same payload, key and request identity; a 409 must stop the caller, without
inventing a new key. Exhausted retries retain that identity for reconciliation.
The [tested client example](../examples/http_client/README.md) checks these cases
and observes the target's independent commit count. 428 means the required key
is missing; 429/503 are surfaced for caller-directed backoff. Current sequential
retry examples do not establish crash-safe ownership; see the planned persistence
and admission work in the adoption plan.

## MCP

`cua mcp` serves the approved capabilities to any agent that speaks the Model
Context Protocol, over stdio. The agent sees business operations, not a
browser:

```text
member_savings_balance(member_id)
open_subaccount(member_id, initial_deposit, idempotency_key, approval_token?)
cua_run_status(run_id)  cua_approve_run(run_id, approval_token)  cua_resume_run(run_id)  cua_abort_run(run_id)
```

There is no `click`, `type` or `press`. Each capability tool's description
is its contract:

- its purpose and outputs;
- its side effect and whether it is idempotent;
- its business outcomes;
- when it will ask for consent.

Its input schema is the capability's typed inputs. A tool that changes
something requires an `idempotency_key`. A tool that may ask for consent takes
an optional `approval_token`, if the client has the `approve` scope. The
annotations say which tools are read-only, destructive or idempotent.

A call goes where a `POST /runs` goes: the registry resolves the version, the
arguments are typed, and the access file's client, tenant and scopes apply.
Replay then runs with no model, and every call is recorded in
`<runs>/.api/requests.jsonl`. The answer is the `ReplayResult`, reduced to what
an agent acts on: `kind`, the code or reason, `outputs`, `side_effect` and
`message`. How the GUI was driven stays in the run directory: locator rungs,
screenshots, the observed screen text. An `escalated` answer adds
`resume_token` and `required_action`, which says what has to happen and which
run tool carries the run on.

Configure your MCP client's stdio server with an absolute command and project root.
`cua --root DIR mcp` also works. Explicit file overrides are relative to the
client's invocation directory, so use absolute overrides when needed.
Substitute the actual checkout path; spaces are allowed inside each argument.
A generic Windows configuration is:

```json
{
  "command": "C:\\work\\inter-cua\\.venv\\Scripts\\cua.exe",
  "args": ["mcp", "--root", "C:\\work\\inter-cua"]
}
```

On POSIX use `/absolute/path/inter-cua/.venv/bin/cua` as the command and
`["mcp", "--root", "/absolute/path/inter-cua"]` as the args. Adapt the outer
keys to your client's schema. The executable must belong to the installed
environment. The root contains the access file, tenants, policy and capabilities;
it does not initialize a new project.

Over stdio the client is whoever started the process, so no key is asked
for. `--client` names the entry in `api/access.yaml` whose tenants,
capabilities and scopes it acts under. By default only capabilities that may
ask for consent run with a handoff (`--handoff consent`). A commit without a
token therefore comes back `escalated` with the session waiting, while a
lookup that gets stuck is a plain `failure`.

## Response handling

HTTP can return `running` with a null result. Poll the returned run ID until
`escalated`, `finished`, `error`, or `lost`. Handle result kind separately from HTTP
status. A business outcome is an answer; an escalation needs human action. A lost
run or unknown side effect needs reconciliation, not a fresh key and automatic
repeat of a write. MCP tool failures can set `isError`; inspect kind, code and
side effect. Protocol errors indicate malformed requests or unsupported methods.

[Documentation index](index.md) · [Project README](../README.md)
