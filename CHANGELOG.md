# Changelog

## Unreleased

- PR17 recovery release preparation: repeatable synthetic service exercises,
  stopped-writer restore/retention regression, Linux/Windows CI and a commit-bound
  evidence checker. Missing, changed, skipped or incomplete reports fail the gate.
  Outstanding PR13/PR15 work and manual deployment recovery gates keep the shared
  service release unqualified.

- PR16 authenticates every operator console route using explicit operator keys
  and tenant-bound read/operate/approve roles, separate from API client grants.
  Forms use identity-bound CSRF tokens; JSON actions require a custom header.
  Verified identities own durable intervention assignments and audit records;
  other operators cannot claim or decide a held request. Normal views hide CDP
  metadata, which is available only to the assigned operator for local tooling.
  Demo and takeover examples use disposable authenticated accounts. Added
  authenticated health/readiness and single-host TLS, rotation and recovery docs.

- PR15 first increment: tenant-scoped `cua maintenance inspect|cleanup`, dry-run
  by default, with configurable screenshot retention, ownership checks and an
  audit log. Retry authority, approval records, discovery provenance, handoffs
  and uncertain outcomes are retained. Authenticated `/metrics` aggregates
  only the caller's visible runs; `/health` exposes status and version only.
  Verified browser termination and abandoned profile cleanup remain pending.

- PR14 bounded run admission and stdio MCP dispatch. Run records distinguish
  `queued` from `running` and persist queue/dispatch timing. Saturation returns
  HTTP 503/Retry-After or a structured MCP retry hint without reserving a new
  key; accepted keys still resolve. Queue expiry starts no application, replay
  budgets begin at dispatch, and graceful shutdown cancels waiting work while
  active runs finish. Ping/status remain available at MCP call capacity.

- PR13's first increment adds SQLite-backed direct replay/resume ownership,
  OS-held execution locks, interruption tombstones and consent consumption
  before risky dispatch. Complete workflow/service envelope migration and
  legacy export tooling remain pending; keep one service per runs directory.

- PR12 atomic runtime JSON publication, serialized file updates and durable
  JSONL appends. Workflow journals reject stale saves; completed replay answers
  retain their first result. Corrupt authoritative records and torn log tails
  fail closed with a reconciliation procedure; HTTP/MCP expose `state_corrupt`.
  OS-held locks replace age-based control lock stealing. Durable invocation
  ownership is added by PR13's first increment; broader envelope/commit recovery
  remains follow-up work.

- PR11 adoption candidate procedure, anonymized developer-trial templates and
  an evidence readiness checker. Missing/failing gates, changed evidence,
  incomplete trials and unresolved blockers prevent readiness; usability timing
  goals remain measurements. Candidate qualification and human trials are pending.

- Contributor setup and checks by change type, GitHub issue/PR templates,
  a support policy and an actionable adoption/service roadmap. Extension recipes
  cover families, surfaces, providers and recoverers with the closed-contract
  integration points; troubleshooting explains provider/model selection,
  diagnostics and uncertain-write reconciliation. The PR11 usability trial and
  release gates require the candidate evidence and independent developer trials.

- Python 3.11/3.14 Linux compatibility gates, focused Windows Chromium checks,
  separate example/security/provider jobs, reusable live adapter contracts and
  a strict manual Windows UIA gate with executed/skipped counts. Routine test
  environments use the provider-free `test` extra. Critical input, binding and
  pixel-refusal branches have targeted coverage gates. Replay keeps substituted
  input/credential text literal; browser action errors use the Surface vocabulary,
  and UIA groups/tabs/windows satisfy named region conditions.

- Runnable CLI, HTTP, MCP, workflow, drift, handoff and Windows UIA examples
  with shared machine-readable scenarios, isolated targets, independent commit
  checks, and text recordings of review, results and evidence. HTTP callers
  preserve request identity across transport retries and stop for conflict,
  escalation or reconciliation; queued responses are forward compatible.

- Independent inventory target and `cua init --template inventory`, starting
  without approvals. The own-app tutorial covers scripted discovery, review,
  typed lookup, business outcomes, input-bound consent, sequential retry and
  human recovery, with an external stock/commit-count oracle and wheel checks.
- One-command PowerShell checks for PR 07, plus Windows test-server process-tree
  shutdown and bounded cleanup retries for temporary project directory locks.

- Optional `anthropic`, `gemini`, and combined `discovery` extras for live
  discovery; the base installation supports deterministic replay, scripted
  discovery, HTTP/MCP, and the no-key demo without provider SDKs.
- A single package version source, CLI version output, distribution metadata,
  development dependency pins, and clean wheel/sdist verification.
- Versioned project configuration and stable path selection from nested or
  unrelated directories.
- Packaged demo/web/Windows starter projects and safe `cua init`, with private
  per-project signing keys and no automatically approved capabilities.
- `cua doctor` with private text/JSON diagnostics and explicit local browser
  and application probes.
- Adoption documentation, finite-decimal validation, current schema export,
  evidence checks, and strict browser test gates.

The current development version is `0.1.0`. Building and checking distributions
does not publish a release. Capability schema versions and approvals are unchanged
by these packaging changes.
