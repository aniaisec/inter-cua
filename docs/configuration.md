# Configuration, paths and secrets

CLI commands select the nearest ancestor containing `cua.toml`. Use global
`cua --root DIR <command>` to select a project from an unrelated directory.
MCP also accepts `cua mcp --root DIR`; conflicting global and MCP roots are
refused. A directory without `cua.toml` retains the existing cwd defaults;
an explicit root without a config uses those defaults under that root.
An invalid root or configuration fails before application interaction.
`cua init` and `cua doctor` remain planned work.

## Project file and lookup

All fields except `version` are optional. Version 1 accepts these settings:

```toml
version = 1
default_tenant = "local"

[paths]
tenants = "tenants"
capabilities = "capabilities"
families = "capabilities/families"
workflows = "workflows"
runs = "evidence/runs"
state = ".cua"
access = "api/access.yaml"
```

Configured relative paths resolve against the selected project root; absolute
paths remain absolute. Unknown fields and unsupported versions are refused.
Named tenant, capability and workflow lookups use the configured directories.
Registry versions live below the capability directory in `registry/`; review
receipts live below `state` in `reviews/`. Schema output follows the configured
capability directory. Discovery checks `families/<app_family>.yaml` before any
model or application work when recording is enabled. `--families-dir` overrides
that directory; `--no-record` allows discovery without a family template.

**Explicit CLI file paths remain relative to the invocation directory.** This
includes `--policy`, `--access`, `--runs-dir`, directory overrides, positional
capability files and workflow files. Use a capability name (for example
`cua --root DIR replay member_savings_balance`) for configured lookup; use an
absolute file path to select a particular file from anywhere. Spaces in paths
work when the shell argument is quoted.

Tenant policy and secret-file references, access-file tenant/policy/key
references, and desktop launch paths remain project-relative, regardless of
where their configuration file is stored. A `--tenant /path/file.yaml` selects
that file without relocating its policy or secrets. Desktop child processes
start with the selected project as their working directory. The runtime
captures absolute paths before dispatching workers and does not change the
parent process's cwd.

For example, both commands select the same project:

```powershell
cua --root "C:\work\my project" catalog --json
cua mcp --root "C:\work\my project"
```

```sh
cua --root "/work/my project" catalog --json
cua mcp --root "/work/my project"
```

## Tenant binding

A capability's `target.app_family` must match the tenant's `app_family`.
`base_url` selects the deployment; `policy` selects its allowlist and masking rules.
An optional `capabilities` list narrows which operations the tenant may run.
`overlay` is not implemented: a non-null value is refused. Desktop tenants use
`uia://<app>` and provide `desktop.launch` as an argument list.
See [local tenant](../tenants/local.yaml) and [desktop tenant](../tenants/desk.yaml).

## Environment precedence

Project selection reads `.env` only from the selected root into an invocation
environment snapshot. Process environment values win; non-empty `.env` values
fill only absent keys. Loading one project does not mutate the process environment
or carry its `.env` values into another project or service.
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
