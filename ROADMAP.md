# Roadmap

This roadmap tracks adoption and single-host service work. Items marked planned
are not available commands or support promises. Sequence expresses dependencies;
there are no delivery dates. The current [support policy](SUPPORT.md),
[changelog](CHANGELOG.md) and [CI gates](docs/CI.md) describe the implemented boundary.

## Adoption foundation

PR01–PR09 implemented correctness fixes, first-run documentation, project paths,
packaged initialization, doctor, optional providers and installed-package checks,
the inventory tutorial, runnable integration examples and compatibility/adapter
gates. PR10 adds contributor/support guidance, templates, troubleshooting and
extension recipes. These changes provide the material for the adoption trial;
they do not establish its usability results.

## Adoption release (PR11)

Status: preparation implemented after PR01–PR10; candidate qualification and
independent developer trials pending. The [release procedure](docs/release/adoption.md)
includes trial templates and a checker for the candidate evidence bundle.

- Have at least two developers unfamiliar with the code follow the guides on
  supported platforms. Record platform, installation method, time to first demo,
  time to the sample custom capability, failed commands and undocumented decisions.
- Aim for a first demo within 15 minutes excluding downloads, and a sample custom
  capability within an hour excluding application access setup. Measure these as
  usability goals; do not advertise them as guarantees.
- Correct trial blockers in the guides or implementation, then rerun the affected
  steps. Retain an anonymized trial record and results.
- Verify a clean wheel outside the checkout, the inventory tutorial, CLI/HTTP/MCP
  examples and all applicable CI/platform gates. Require live Windows evidence
  for a desktop support claim. Check platform limits, changelog and absence of
  undocumented source edits before preparing a release candidate.

Completion requires recorded trial and release evidence. Package publication is
a separate release action.

## Single-host service hardening

These increments can begin independently of the adoption trial where dependencies
allow. Preserve exact-content approval, model-free replay, typed business outcomes,
pre-action feature refusal and explicit uncertainty after a possible commit.

| Increment | Depends on | Deliverable and acceptance evidence |
|---|---|---|
| PR12: atomic persistence (implemented) | Project paths (PR03) | Shared same-directory atomic JSON replacement, OS-held locks, stale workflow-save refusal and corrupt-state handling. Fault/concurrency regressions and [reconciliation instructions](docs/operations.md#corrupt-or-interrupted-state); durable invocation ownership is still PR13. |
| PR13: durable invocation ownership | PR12 | Transactional local SQLite claims shared by CLI, workflow, HTTP and MCP; pinned content and result identity. Race processes and interrupt at commit boundaries; show one observed commit or explicit uncertainty, never automatic repetition of an unknown write. Include migration/rollback rules. |
| PR14: bounded admission | PR13 | Bounded workers/pending runs/MCP dispatch, truthful persisted `queued` state and overload responses. Saturation starts no extra UI sessions and preserves accepted retry identities; verify shutdown and queue expiry. |
| PR15: cleanup and retention | PR13, PR14 | Scoped inspect/cleanup plans with dry-run default, ownership checks, retention policies and operations metrics. Prove cleanup preserves active sessions, unresolved commits, provenance and retry-safety tombstones. |
| PR16: operator service | PR13–PR15 | Authenticated tenant-bound operator roles, protected screens/actions, exclusive intervention ownership and deployment instructions. Verify operator isolation, audited transitions and real-screen hand-back checks. |
| PR17: service release exercise (preparation implemented) | PR12–PR16 | [Repeatable synthetic recovery exercise and evidence checker](docs/release/service.md) for concurrency, saturation, operator isolation, expired handoffs, process loss, shutdown, backup/restore and retention. PR13/PR15 completion and reviewed deployment evidence remain required before claiming the hardened single-host boundary. |

Durable local ownership cannot make an application UI and a local database one
transaction. Tests must measure external effects and expose uncertain outcomes.
See [operations](docs/operations.md) for current persistence and concurrency limits.

## Later extensions

Gather a concrete target and a verification oracle before selecting another
surface. A feasibility spike should end with shared conformance coverage and one
complete application example. New browser/native adapters require changes to
the closed core contract; see [extension recipes](docs/extending.md).

Windows masked screenshots, Windows handoff, authenticated remote browser viewing,
tenant overlays, broader application evaluation and distributed workers remain
separate proposals. Distributed execution needs a worker/session-affinity and
lease design with evidence beyond the single-host gates.

Propose a bounded contribution through [GitHub issues](https://github.com/aniaisec/inter-cua/issues)
with its use case, dependencies and acceptance evidence. Follow
[CONTRIBUTING.md](CONTRIBUTING.md); report security problems [privately](SECURITY.md).
