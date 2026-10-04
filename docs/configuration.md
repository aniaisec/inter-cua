# Configuration, paths and secrets

Run CLI and HTTP commands from the repository root. There is currently no `cua.toml`,
ancestor project discovery, `cua init`, or `cua doctor`. These are planned work.
Only MCP currently supports `cua mcp --root DIR`; it changes the process working
directory before loading `.env` and configuration.

## File lookup

| Setting | Current default / resolution |
|---|---|
| Tenant | `--tenant local` reads `tenants/local.yaml`; a path can select a file |
| Policy | Explicit `--policy`, else tenant `policy`, else `policies/default.yaml` |
| Family template | `capabilities/families/<app_family>.yaml` during discovery recording |
| Working capabilities | `capabilities/`; named invocation selects a registered approved version |
| Registered versions | `capabilities/registry/` with lifecycle ledger |
| Discovery / replay evidence | `evidence/runs/`, overridden by `--runs-dir` where exposed |
| HTTP/MCP authorization | `api/access.yaml`, overridden by `--access` |
| Review receipts and local keys | `.cua/` |
| Workflows | `workflows/` |

Relative policy, secret-file, desktop launch, and explicit CLI paths are relative
to the current working directory, not the tenant file's directory. Changing cwd
can change which files are loaded. Use absolute paths where a flag supports them,
or launch from the root; `--tenant /path/file.yaml` does not relocate the rest of
the project. For MCP, resolve these paths relative to its `--root`.

## Tenant binding

A capability's `target.app_family` must match the tenant's `app_family`.
`base_url` selects the deployment; `policy` selects its allowlist and masking rules.
An optional `capabilities` list narrows which operations the tenant may run.
`overlay` is not implemented: a non-null value is refused. Desktop tenants use
`uia://<app>` and provide `desktop.launch` as an argument list.
See [local tenant](../tenants/local.yaml) and [desktop tenant](../tenants/desk.yaml).

## Environment precedence

Commands that use credentials load `.env` from the current working directory.
Process environment values win; non-empty `.env` values fill only absent keys.
The loader accepts simple `KEY=VALUE` lines, comments and surrounding quotes,
not shell expansion. Copy [.env.example](../.env.example) only if `.env` does not
already exist, then edit locally; it is gitignored.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | Live discovery provider credentials |
| `CUA_LLM` | Provider preference for `--llm auto`; explicit provider overrides |
| `CUA_MODEL` / `CUA_GEMINI_MODEL` | Default selected-provider model; `--model` overrides |
| `CUA_SECRET_MOCKCORE_OPERATOR` | Synthetic mock login, `operator:operator` |
| `CUA_HEADED` | Show browser windows in tests |
| `CUA_API_KEY_BALANCE_READER` | Enable optional read-only HTTP client |

With auto-selection an explicit preference is used first; otherwise an Anthropic
key is preferred when both provider keys exist. Scripted discovery, replay,
operator and routine tests need no model key. Model IDs are provider-specific;
use an available ID rather than assuming a sample default is always offered.

## Secret references and policy

Tenant `secrets` map a reference name to an environment variable or file and a
format such as `username:password`. Artifacts carry `secret://{tenant.id}/...`,
never the value. References can access only their own tenant; `cua/` secrets are
system secrets and cannot be offered to discovery. The approval-signing key allows
consent on that tenant's behalf. Local synthetic keys are created on first use;
real deployments should mount protected keys and restrict file access.

Policy controls origins, paths, actions, credential sinks, risky actions, masks,
scrub patterns and optional vision. A browser origin allowance also restricts
network egress. See [security invariants](architecture.md#security) and
[threat model](THREAT_MODEL.md) before changing the boundary.

[Documentation index](index.md) · [Project README](../README.md)
