# Cost break-even

When discovering a capability once and replaying it costs less than asking a model to operate the UI on every invocation.

```text
traditional CUA:  N x baseline per run
inter-cua:        discovery + N x replay per run
break-even:       N* = discovery / (baseline per run - replay per run)
```

Sessions: `bench_01M3NQEZG8M9HR54RZ849KKQ3Z`, `bench_01M3PNNZW5ABDXK197AV98CM94`. Models: `gemini-3.8-flash`.

## Prices

- **Model**: each run's own estimate, its tokens times `bench/pricing.yaml` when it ran.
- **Browser**: $0.08925 per browser-hour, times each run's wall clock. The baseline holds its browser while it waits on the model.
- **Storage**: $0.023 per GiB-month for 12 month(s), times the evidence each run left (log, observations, screenshots, trace).
- Source: AWS list prices, us-east-1 on-demand c7i.large (2 vCPU, 4 GiB) Linux host and S3 Standard; not re-checked against a bill (as of 2026-09-29). Configured, not measured; the seconds and bytes they multiply are measured.

## Per invocation

Mean cost per run, by part. Discovery is per capability obtained: every attempt, divided by the attempts that produced one.

| Capability | Strategy | Runs | Mean s | Evidence KiB/run | Model | Browser | Storage |
|---|---|---:|---:|---:|---:|---:|---:|
| member_savings_balance | Repeated LLM (baseline) | 120 | 21.59 | 153 | $0.043876 | $0.000535 | $0.000040 |
| member_savings_balance | Discovery (once) | 1 | 15.00 | 147 | $0.030085 | $0.000372 | $0.000039 |
| member_savings_balance | inter-cua replay | 120 | 7.67 | 319 | $0.000000 | $0.000190 | $0.000084 |
| open_subaccount | Repeated LLM (baseline) | 60 | 38.71 | 278 | $0.074347 | $0.000960 | $0.000073 |
| open_subaccount | Discovery (once) | 1 | 39.84 | 278 | $0.074384 | $0.000988 | $0.000073 |
| open_subaccount | inter-cua replay | 60 | 7.12 | 147 | $0.000000 | $0.000177 | $0.000039 |

Tasks in each capability's mix (both strategies ran them):

- member_savings_balance: lookup-renamed-button, lookup-server-error, lookup-success, lookup-unknown-member
- open_subaccount: open-retried-request, open-with-consent

## Break-even

| Capability | Priced | Discovery | Baseline/run | Replay/run | Saving/run (95% CI) | N* (95% range) | Model spend only |
|---|---|---:|---:|---:|---:|---:|---:|
| member_savings_balance | model + browser + storage | $0.030496 | $0.044452 | $0.000274 | $0.044178 ($0.041236 to $0.047061) | **1** (1 to 1) | 1 |
| open_subaccount | model + browser + storage | $0.075445 | $0.075380 | $0.000215 | $0.075165 ($0.074750 to $0.075522) | **2** (1 to 2) | 2 |

N* is the first whole number of invocations after which discover-then-replay has spent less in total than asking the model every time (the exact ratio is in `break_even.json`). *Model spend only* is the same calculation on the model part alone. The range comes from the bootstrap interval of the saving per run; discovery ran once, so its own spread is not in it.

## At volume

| Capability | Invocations | Traditional CUA | inter-cua | Saved | inter-cua / traditional |
|---|---:|---:|---:|---:|---:|
| member_savings_balance | 10 | $0.44 | $0.03 | $0.41 | 7.5% |
| member_savings_balance | 100 | $4.45 | $0.06 | $4.39 | 1.3% |
| member_savings_balance | 1,000 | $44.45 | $0.30 | $44.15 | 0.7% |
| member_savings_balance | 10,000 | $444.52 | $2.77 | $441.74 | 0.6% |
| open_subaccount | 10 | $0.75 | $0.08 | $0.68 | 10.3% |
| open_subaccount | 100 | $7.54 | $0.10 | $7.44 | 1.3% |
| open_subaccount | 1,000 | $75.38 | $0.29 | $75.09 | 0.4% |
| open_subaccount | 10,000 | $753.80 | $2.23 | $751.57 | 0.3% |

## What this leaves out

- **A person's review.** A discovered capability is approved by a person before it replays. That is a one-time cost per capability version, not priced here.
- **Re-discovery after drift.** When the UI changes, replay stops safely (`LOCATOR_UNRESOLVED`) and the capability has to be discovered again: one more discovery cost each time. Re-discovered every K invocations, a capability adds discovery / K to each one.
- **What follows a safe stop.** A run that stopped, and handed its work to a person, is priced as the run it was. The person's time is not in it.
- **Prices move.** Model prices come from the table with their date; the host and storage prices are configured. These are estimates, not billing.
- **One mock app on one machine.** The seconds and bytes are this app's and this machine's; another application moves every number.
