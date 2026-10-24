# Adoption release candidate (PR11)

Status: preparation implemented; candidate qualification and two independent
developer trials remain pending. No release has been published or qualified by
this document. The package remains at its current development version until a
maintainer chooses the candidate version in `src/cua/__init__.py` and the changelog.

## Prepare one candidate

The snapshot preparer builds from a full reviewed commit ID, keeping workspace
edits and untracked files out of the candidate. It creates a new private bundle
with source archive, wheel/sdist, hashes, build logs, provenance, a pending release
record and two blank trial templates. For example, in PowerShell or POSIX:

```text
python -m scripts.ci.prepare_adoption_candidate --commit FULL_40_CHARACTER_COMMIT --out artifacts/adoption/candidate1
```

Install `.[test]` first for the build tools. The source is extracted into a
temporary directory and built with `--no-isolation`; the preparer refuses links
and paths escaping the snapshot. Only a committed snapshot is built, even when
the invoking checkout has local edits. Existing output directories are refused.
Run this only for a reviewed commit: building executes that snapshot's build
backend and validation helpers. The package version is retained from its source;
this command does not choose a new release version, tag or publish artifacts.

Add `--verify-installed` to run the snapshot's existing fresh wheel **and**
sdist-built wheel checks, including browser probing, the seven-stage no-key demo
and inventory consent/retry scenario outside the checkout. This option installs
dependencies and Chromium as needed. Linux browser system libraries must already
be available. It does not replace the full CI matrix or the live Windows gate.

Exit 0 means `candidate_prepared`, with `release_status: not_qualified`. Every
release gate and both trials still need review. A failed command returns exit 1,
retains its logs and writes a failed summary/provenance record. Preserve failed
attempts and rerun into a new directory; do not turn a timeout or skipped test
into passing evidence. Keep logs, source archives and runtime material private.
The [initial qualification report](candidates/0.1.0-6fda797.md) records the first
observations; it is not a qualified release or evidence for a different commit.

Choose a reviewed, clean commit after PR01–PR10. Follow
[packaging](../packaging.md#wheel-and-sdist-verification) to build wheel/sdist and
verify clean installations outside the checkout, including the no-key demo and
inventory scenario. Use the actual archive names for the chosen version. Keep
the commit, package version, wheel SHA-256 and complete CI run URL together.
Rebuild/requalify after candidate changes; do not mix old passing reports with a
new wheel. Building and checking do not publish a package or create a Git tag.

Copy [adoption-record.template.json](adoption-record.template.json) to a private
evidence directory as `record.json`. Put the wheel and sanitized reports beneath
that directory. Paths resolve relative to `record.json`; traversal and symlinks
outside that directory are refused. Replace the zero commit/hash placeholders
with actual values. SHA-256 can be obtained with `Get-FileHash -Algorithm SHA256`
in PowerShell (lowercase the result) or `sha256sum` on Linux.

## Review every gate

Each gate entry starts pending. After inspecting its results, set `status` to
`passed` and add `evidence: {"path": "reports/NAME.md", "sha256": "HASH"}`.
A report must name the candidate commit, exact commands, platform/Python, date,
result counts and CI run/job URLs where applicable. Review every matrix instance,
not just the first green job. Do not store secrets or raw runtime directories.

| Record gate | Required evidence |
|---|---|
| quality | Both Linux Python versions, lint/format, both typing targets, unit suite and critical branch coverage |
| browser | Both Linux versions, strict Chromium/Surface contract, benchmark checks |
| examples | All CLI/HTTP/MCP/workflow/drift/handoff scenarios, independent commit checks |
| security | Strict security tests, negative controls and security CLI report |
| windows | Hosted Windows unit and real Chromium/inventory/Surface gates |
| providers | Synthetic SDK checks with network refused |
| installed | Entire wheel/sdist/extras matrix; fresh non-editable base demo and inventory outside checkout |
| desktop | Interactive Windows [manual gate](../CI.md#live-windows-release-gate), JUnit and summary with executed/skipped counts |
| documentation | Review README shell commands, inventory walkthrough, CLI/HTTP/MCP guides, platform limits and installation/configuration changelog against this candidate |

See [CI](../CI.md) for exact job commands and allowed desktop skips. Desktop
evidence is required for the current Windows UIA support boundary. A skipped
GUI run or a Windows type check cannot substitute for it. Documentation review
also verifies no undocumented source edits were needed and preserves the stated
concurrency/recovery limitations. Some PR12–PR17 hardening is implemented, but
the [shared-service release](service.md) remains unqualified.

## Run two developer trials

Recruit at least two developers unfamiliar with the code and use
[trial-template.md](trial-template.md). Use supported Windows or Linux environments
and record the actual platform/shell; seek both when available. Each developer
uses the same candidate wheel in a fresh environment outside the checkout,
follows [getting started](../getting-started.md) and the
[own-app tutorial](../your-first-application.md), and records failures and decisions.
Facilitators observe and record help instead of silently correcting commands.

Append one object per completed participant to `trials`, for example (replace
all example values with observations):

```json
{
  "participant": "developer-A",
  "platform": "windows",
  "unfamiliar_with_code": true,
  "completed": true,
  "no_source_edits": true,
  "demo_minutes": 12.5,
  "capability_minutes": 48.0,
  "evidence": {"path": "trials/developer-A.md", "sha256": "REPLACE_WITH_SHA256"}
}
```

Minutes exclude only the documented download/access-setup pauses. Exceeding the
15/60-minute goals is reported for review, not hidden or treated as a guarantee.
Resolve blockers, record fixes and retest affected steps before removing entries
from `open_blockers`. Preserve sanitized original observations and retest results.

## Check readiness and retain evidence

From a checkout with the test dependencies installed, in either shell:

```sh
python -m scripts.ci.check_adoption_release PATH/TO/record.json
```

Exit 1 means malformed, missing, changed, failing or incomplete evidence. Exit 0
emits JSON with `ready_for_review` and participants who missed timing goals. The
checker requires all nine named gates, matching wheel metadata, content hashes,
two distinct eligible completed participants and no open blockers. It checks
record consistency; a maintainer must inspect report contents, commit provenance,
CI matrix completeness and truthful human observations. It does not independently
execute tests, authenticate participants or certify arbitrary attached reports.

Retain the manifest, wheel, sanitized reports and trial records as one reviewable
bundle. Link that bundle and candidate CI run from the PR/release review; keep
private runtime evidence local. Summarize trial measurements, platform coverage,
fixed failures and remaining nonblocking friction in the candidate release notes.
Publication requires a separate maintainer release action after this review.
