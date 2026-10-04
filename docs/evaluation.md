# Evaluation and measured results

## Measured results

The [statistical evaluation](../bench/reports/statistical/summary.md) compares
180 repeated-model invocations with 180 replays: six paired tasks, 30 runs
per task, Gemini 3.8 Flash, Chromium on Windows, September 29, 2026. Both
strategies use the same mock app, credentials, policy and injected faults.

| Metric | Repeated LLM | Deterministic replay |
|---|---:|---:|
| Exact expected result (95% CI) | 83.9% (78–89) | 83.3% (77–88) |
| Median latency | 24.35 s | 7.62 s |
| P95 latency | 54.20 s | 13.94 s |
| Mean model calls per invocation | 7.97 | 0 |
| Estimated model cost per invocation | $0.0540 | $0 |
| Wrong results | 29 | 0 |
| Duplicate commits | 29 | 0 |
| Runs ending needing a person | 33.3% | 33.3% |

Replay reduced model usage and latency in this experiment. Its exact-result
rate was similar: on the renamed-button task, the model adapted while every
replay stopped safely. The baseline's wrong results were duplicate commits
on retried requests. No person acted during these unattended benchmark runs.
The report includes uncertainty intervals and excludes 30 provider failures
that were rerun in a second session. This synthetic suite on one application
does not establish performance on other applications.

Replay still uses compute and stores evidence. The
[cost report](../bench/reports/break_even.md), using configured infrastructure
prices and measured evidence, puts replay at about $0.0002–0.0003 per run and
model-plus-infrastructure break-even at one or two invocations, depending on
the capability. Human review and recovery costs are separate; the
[human intervention report](../bench/reports/humans.md) records those observed
times and labels the operator's hourly price as an assumption.

## Security results

The [security benchmark](../bench/security/reports/summary.md) staged 22 attacks
with a scripted model that follows hostile screen instructions, then judged
their effects against the real components.

| Measured outcome | Result |
|---|---:|
| Attacks blocked or contained as expected | 22/22 |
| Unsafe actions / secret exposures | 0 / 0 |
| Policy / approval bypasses | 0 / 0 |
| Tenant isolation failures | 0 |

These results cover the declared fixtures, including exfiltration, tampered
artifacts, forged approval, reused consent and cross-tenant requests. See the
[threat model](THREAT_MODEL.md) and [security design](architecture.md#security) for the
controls and limits.

[Documentation index](index.md) · [Project README](../README.md)
