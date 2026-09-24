# Benchmark summary

## Setup

- `bench_01M388TQM8J5S38B4HTPFXZ8V5` (2026-09-23T23:15:20.840Z): suite `core`, 92 runs, model `gemini`; commit `1cba3ef` (dirty); Python 3.14.5, Playwright 1.63.0, Chromium 153.0.8010.12; Windows-11-10.0.26200-SP0
- Models that answered: gemini-3.8-flash

## By strategy

| Strategy | Runs | Success (95% CI) | Safe stop | Wrong | Escalation | Human | Median s | P95 s | LLM calls/run | Tokens/run | Cost/run | Duplicate commits |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Repeated LLM (baseline) | 45 | 84.4% (71-92) | 11.1% | 4.4% | 40.0% | 0.0% | 23.415 | 48.245 | 8.02 | 67848.4 | $0.053089 | 2 |
| inter-cua discovery (once) | 2 | 100.0% (34-100) | 0.0% | 0.0% | 0.0% | 0.0% | 21.618 | 28.891 | 8.0 | 64919.0 | $0.051008 | 0 |
| inter-cua replay | 45 | 80.0% (66-89) | 20.0% | 0.0% | 33.3% | 0.0% | 10.03 | 23.691 | 0.0 | 0.0 | $0.000000 | 0 |

## Model-cost break-even

Discovery cost $0.051008 once; the baseline costs $0.053089 and replay $0.000000 per invocation. Discover-then-replay has spent less on the model after **1** invocation(s).
Model spend only, from the configured price table; an estimate, not billing.

## By scenario tag

| Tag | Strategy | Runs | Success | Safe stop | Wrong |
|---|---|---:|---:|---:|---:|
| business_outcome | Repeated LLM (baseline) | 9 | 77.8% | 22.2% | 0.0% |
| business_outcome | inter-cua replay | 9 | 100.0% | 0.0% | 0.0% |
| drift | Repeated LLM (baseline) | 6 | 100.0% | 0.0% | 0.0% |
| drift | inter-cua replay | 6 | 50.0% | 50.0% | 0.0% |
| failure | Repeated LLM (baseline) | 6 | 50.0% | 50.0% | 0.0% |
| failure | inter-cua replay | 6 | 50.0% | 50.0% | 0.0% |
| recovery | Repeated LLM (baseline) | 15 | 80.0% | 20.0% | 0.0% |
| recovery | inter-cua replay | 15 | 60.0% | 40.0% | 0.0% |
| security | Repeated LLM (baseline) | 3 | 100.0% | 0.0% | 0.0% |
| security | inter-cua replay | 3 | 100.0% | 0.0% | 0.0% |
| side_effects | Repeated LLM (baseline) | 15 | 73.3% | 13.3% | 13.3% |
| side_effects | inter-cua discovery (once) | 1 | 100.0% | 0.0% | 0.0% |
| side_effects | inter-cua replay | 15 | 80.0% | 20.0% | 0.0% |
| success | Repeated LLM (baseline) | 6 | 100.0% | 0.0% | 0.0% |
| success | inter-cua discovery (once) | 2 | 100.0% | 0.0% | 0.0% |
| success | inter-cua replay | 6 | 100.0% | 0.0% | 0.0% |

## By task

| Task | Strategy | Runs | Success | Safe stop | Wrong | Median s | P95 s | LLM calls | Tokens/run | Cost/run | Outcomes |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| lookup-ambiguous-button | Repeated LLM (baseline) | 3 | 100.0% | 0.0% | 0.0% | 18.449 | 23.121 | 18 | 37428.7 | $0.029950 | done x3 |
| lookup-ambiguous-button | inter-cua replay | 3 | 100.0% | 0.0% | 0.0% | 10.194 | 11.973 | 0 | 0.0 | $0.000000 | success x3 |
| lookup-interstitial | Repeated LLM (baseline) | 3 | 100.0% | 0.0% | 0.0% | 21.317 | 23.006 | 21 | 47453.3 | $0.037275 | done x3 |
| lookup-interstitial | inter-cua replay | 3 | 100.0% | 0.0% | 0.0% | 6.845 | 7.057 | 0 | 0.0 | $0.000000 | success x3 |
| lookup-persistent-interstitial | Repeated LLM (baseline) | 3 | 0.0% | 100.0% | 0.0% | 13.333 | 15.353 | 15 | 26611.7 | $0.021351 | escalated:DEAD_END x3 |
| lookup-persistent-interstitial | inter-cua replay | 3 | 0.0% | 100.0% | 0.0% | 3.074 | 3.15 | 0 | 0.0 | $0.000000 | failure:RECOVERY_EXHAUSTED x3 |
| lookup-renamed-button | Repeated LLM (baseline) | 3 | 100.0% | 0.0% | 0.0% | 14.461 | 15.16 | 18 | 37102.3 | $0.029510 | done x3 |
| lookup-renamed-button | inter-cua replay | 3 | 0.0% | 100.0% | 0.0% | 9.827 | 10.874 | 0 | 0.0 | $0.000000 | failure:LOCATOR_UNRESOLVED x3 |
| lookup-restricted-member | Repeated LLM (baseline) | 3 | 100.0% | 0.0% | 0.0% | 24.305 | 31.924 | 21 | 50188.0 | $0.045086 | escalated:STUCK x3 |
| lookup-restricted-member | inter-cua replay | 3 | 100.0% | 0.0% | 0.0% | 5.019 | 5.25 | 0 | 0.0 | $0.000000 | business_outcome:PERMISSION_DENIED x3 |
| lookup-server-error | Repeated LLM (baseline) | 3 | 100.0% | 0.0% | 0.0% | 36.362 | 48.245 | 25 | 68283.3 | $0.058369 | escalated:STUCK x3 |
| lookup-server-error | inter-cua replay | 3 | 100.0% | 0.0% | 0.0% | 5.077 | 5.963 | 0 | 0.0 | $0.000000 | failure:APP_ERROR x3 |
| lookup-session-expired | Repeated LLM (baseline) | 3 | 100.0% | 0.0% | 0.0% | 23.582 | 32.289 | 33 | 109882.3 | $0.085509 | done x3 |
| lookup-session-expired | inter-cua replay | 3 | 100.0% | 0.0% | 0.0% | 10.973 | 11.245 | 0 | 0.0 | $0.000000 | success x3 |
| lookup-slow-load | Repeated LLM (baseline) | 3 | 100.0% | 0.0% | 0.0% | 18.805 | 24.303 | 18 | 37286.3 | $0.029774 | done x3 |
| lookup-slow-load | inter-cua replay | 3 | 100.0% | 0.0% | 0.0% | 11.646 | 11.662 | 0 | 0.0 | $0.000000 | success x3 |
| lookup-success | Repeated LLM (baseline) | 3 | 100.0% | 0.0% | 0.0% | 19.359 | 19.44 | 18 | 37072.0 | $0.028572 | done x3 |
| lookup-success | inter-cua discovery (once) | 1 | 100.0% | 0.0% | 0.0% | 14.345 | 14.345 | 6 | 37184.0 | $0.029856 | done x1 |
| lookup-success | inter-cua replay | 3 | 100.0% | 0.0% | 0.0% | 8.862 | 9.699 | 0 | 0.0 | $0.000000 | success x3 |
| lookup-unknown-member | Repeated LLM (baseline) | 3 | 100.0% | 0.0% | 0.0% | 24.327 | 29.983 | 21 | 49788.0 | $0.043995 | escalated:STUCK x3 |
| lookup-unknown-member | inter-cua replay | 3 | 100.0% | 0.0% | 0.0% | 7.811 | 8.297 | 0 | 0.0 | $0.000000 | business_outcome:NOT_FOUND x3 |
| open-retried-request | Repeated LLM (baseline) | 3 | 33.3% | 0.0% | 66.7% | 25.557 | 30.449 | 30 | 93141.0 | $0.072736 | done x3 |
| open-retried-request | inter-cua replay | 3 | 100.0% | 0.0% | 0.0% | 0.007 | 22.672 | 0 | 0.0 | $0.000000 | success x3 |
| open-slow-confirm | Repeated LLM (baseline) | 3 | 100.0% | 0.0% | 0.0% | 33.119 | 34.337 | 30 | 93290.7 | $0.072689 | done x3 |
| open-slow-confirm | inter-cua replay | 3 | 0.0% | 100.0% | 0.0% | 25.139 | 26.709 | 0 | 0.0 | $0.000000 | failure:TIMEOUT x3 |
| open-validation-error | Repeated LLM (baseline) | 3 | 33.3% | 66.7% | 0.0% | 54.245 | 80.328 | 36 | 160723.7 | $0.109214 | escalated:DEAD_END x2, escalated:STUCK x1 |
| open-validation-error | inter-cua replay | 3 | 100.0% | 0.0% | 0.0% | 15.686 | 16.043 | 0 | 0.0 | $0.000000 | business_outcome:VALIDATION_ERROR x3 |
| open-with-consent | Repeated LLM (baseline) | 3 | 100.0% | 0.0% | 0.0% | 29.101 | 32.732 | 30 | 93006.3 | $0.072543 | done x3 |
| open-with-consent | inter-cua discovery (once) | 1 | 100.0% | 0.0% | 0.0% | 28.891 | 28.891 | 10 | 92654.0 | $0.072160 | done x1 |
| open-with-consent | inter-cua replay | 3 | 100.0% | 0.0% | 0.0% | 18.168 | 21.197 | 0 | 0.0 | $0.000000 | success x3 |
| open-without-consent | Repeated LLM (baseline) | 3 | 100.0% | 0.0% | 0.0% | 22.331 | 25.672 | 27 | 76469.0 | $0.059759 | escalated:NEEDS_APPROVAL x3 |
| open-without-consent | inter-cua replay | 3 | 100.0% | 0.0% | 0.0% | 16.456 | 16.925 | 0 | 0.0 | $0.000000 | failure:POLICY_BLOCKED x3 |

## Reading this

- **Success**: the correct answer was delivered — the right outputs or the right business outcome — with the right side effects. Each task states its ground truth independently of either strategy (`bench/tasks/`).
- **Safe stop**: no answer, and nothing wrong done: the run failed or escalated cleanly. Work a person picks up, not a mistake.
- **Wrong**: a wrong answer, or a commit that should not have happened (without consent, or a second time for the same request).
- The baseline has no typed channel for a business outcome; a stop whose stated reason matches the task's declared pattern counts as the right answer. This judge is a heuristic and is declared per task.
- Costs come from `bench/pricing.yaml` and are estimates. Replay makes no model call, so its model cost is zero; its cost is browser time, shown as latency.
