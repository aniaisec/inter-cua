# Changelog

## Unreleased

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
