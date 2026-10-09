# Your first application

Use the [inventory walkthrough](../examples/inventory/README.md) to discover,
review, approve and replay a second application without a provider key. It starts
from `cua init inventory-project --template inventory`; the generated guide
contains copyable PowerShell and POSIX commands and explanations of every
tenant, policy, family and outcome-detector field.

The sample covers item lookup with a second input, a missing item, insufficient
stock, validation, a consent-requiring stock adjustment, an independently checked
commit count, sequential idempotent retry, session expiry, renamed/ambiguous
controls and a changed screen requiring human takeover. Discovery records drafts
through the same loop as live discovery; replay makes no model calls. See the
[full guide](../src/cua/resources/templates/inventory/README.md) for commands,
expected exit codes, evidence, cleanup and how to replace its URL, fields and
policy with your own application without editing inter-cua source.

For a blank application instead, initialize `cua init my-project --template web`
and replace its explicit placeholders. Begin with a read-only operation on a test
deployment you can inspect and reset. Create and validate your family before
discovery: automatic recording refuses a missing or invalid family before model
work or UI launch. Use `--no-record` only for deliberate discovery without a draft.

Approval binds to the exact content most recently described. Invocation consent
is separate and bound to the specific inputs, version and tenant. Review every
credential sink, locator rung, checkpoint, outcome detector, retry and irreversible
step. One happy-path discovery cannot infer every business failure or recovery
condition; test those against an independent application-state oracle.

Commands select the nearest ancestor `cua.toml`; use `cua --root DIR <command>`
from elsewhere. See [configuration](configuration.md), [CLI and consent](cli.md),
[families and recoverers](extending.md), [platform constraints](platforms.md) and
[optional providers](packaging.md). When reliable, choose
[CLI, HTTP or MCP](integrations.md) or compose approved operations in a workflow.

[Documentation index](index.md) · [Project README](../README.md)
