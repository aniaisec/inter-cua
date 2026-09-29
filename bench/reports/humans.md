# Human intervention

175 runs read from `evidence`.

| Run kind | Runs | Requiring a person | A person acted | Interventions | Human wait s | Browser actions |
|---|---:|---:|---:|---:|---:|---:|
| all | 175 | 28 (16.0%) | 24 (13.7%) | 32 | 5889.0 | 11 |
| replay | 132 | 22 (16.7%) | 22 (16.7%) | 26 | 5839.6 | 11 |
| discovery | 43 | 6 (14.0%) | 2 (4.7%) | 6 | 49.4 | 0 |

| How it ended | Count | Mean s | Median s | P95 s | Queued s (mean) | In control s (mean) | Browser actions | Decided by | Mean cost |
|---|---:|---:|---:|---:|---:|---:|---:|---|---:|
| approval | 6 | 18.88 | 17.53 | 40.85 | 18.87 | 0.01 | 0 | anil x3, evidence-bot x1, reviewer x1, demo-presenter x1 | $0.2098 |
| recovery (the automation carried on) | 8 | 6.85 | 4.37 | 17.70 | 6.44 | 0.40 | 1 | cua-resume x4, api:local-agent x2, evidence-bot x1, anil x1 | $0.0761 |
| manual completion (the person finished) | 5 | 45.13 | 34.36 | 118.19 | 32.74 | 12.39 | 10 | anil x2, evidence-bot x1, reviewer x1, demo-presenter x1 | $0.5015 |
| abort (a person stopped it) | 6 | 5.71 | 4.53 | 18.26 | 5.71 | 0.00 | 0 | api:local-agent x4, anil x2 | $0.0634 |
| expired (nobody answered in time) | 1 | 5407.07 | 5407.07 | 5407.07 | 5407.07 | 0.00 | 0 | timeout x1 | $60.0785 |
| returned (handed back, no checkpoint held; asked again) | 2 | 26.99 | 26.99 | 41.04 | 15.83 | 11.16 | 0 | anil x2 | $0.2999 |
| not asked (no channel to a person) | 4 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0 | - x4 | $0.0000 |

Answered by someone (27): mean 17.85 s, p95 41.04 s of the run's time on them; about $0.1983 each at $40/h.

| Decided by | Count | Mean s | P95 s | How it ended |
|---|---:|---:|---:|---|
| anil | 10 | 29.81 | 118.19 | approval x3, manual_completion x2, abort x2, returned x2, recovery x1 |
| api:local-agent | 6 | 2.34 | 6.28 | abort x4, recovery x2 |
| cua-resume | 4 | 9.99 | 17.70 | recovery x4 |
| demo-presenter | 2 | 39.91 | 40.85 | manual_completion x1, approval x1 |
| evidence-bot | 3 | 1.24 | 2.09 | recovery x1, approval x1, manual_completion x1 |
| reviewer | 2 | 23.15 | 34.36 | approval x1, manual_completion x1 |

Time is the run's time on the person, from its control transitions: queued until someone took the request, in control while they held it. An unanswered request counts until the run was resumed. A person acted means a browser action, a handback or an abort; a run requiring a person asked for one or ended needing one. Rows decided by a scripted operator show the mechanism, not a person's time.
