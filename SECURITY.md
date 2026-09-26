# Security

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/aniaisec/inter-cua/security/advisories/new),
not in a public issue. Include what you did, what happened, and what you
expected. A failing scenario for `bench/security/scenarios.yaml` is the most
useful form a report can take.

Only the latest commit on `main` is supported.

## The security model in one paragraph

The application's screens are hostile input and the model that reads them
is steerable. The model proposes one action at a time and holds no secret, no
token, and no way to navigate except through the application. Every action it
proposes, and every step a capability replays, passes deterministic checks:

- the action policy, covering origins, paths, link destinations, downloads,
  and risky commits that need a runner-held approval;
- a credential sink that types a secret only into a sign-on field;
- a browser egress guard that aborts requests to any other origin,
  including redirect hops;
- masking and scrubbing of secrets and personal data;
- an approval gate that runs only capabilities whose approval is recorded
  for their exact content;
- signed, single-use approval tokens bound to one capability version, one
  tenant, one set of inputs, one person and one commit.

The full model, with the residual risks, is in
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

## Checking it

```bash
cua security run            # every scenario; starts its own mock app and browser
cua security run --offline  # only the scenarios that need no browser
```

Each attack is staged against the real components with a scripted model that
follows every injected instruction, and judged by its effects. The report is
written to [bench/security/reports/summary.md](bench/security/reports/summary.md).
It gives `attack_count`, `blocked_count`, `unsafe_action_count`,
`secret_exposure_count`, `policy_bypass_count`, `approval_bypass_count` and
`tenant_isolation_failures`. The run exits 1 if any attack was not blocked.
