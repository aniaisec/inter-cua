# Adoption trial record

Status: pending. Copy this file for each participant. Use anonymous participant
IDs; exclude credentials, signing keys, private URLs, screenshots and personal data.
Do not mark completion until the participant has performed the tasks.

| Field | Observed value |
|---|---|
| Participant ID | |
| Date | |
| Unfamiliar with this code before trial? | |
| Candidate commit / package version / wheel SHA-256 | |
| OS / version / Python / shell | |
| Installation command (clean wheel, outside checkout) | |
| Demo elapsed minutes / excluded download minutes | |
| Custom capability elapsed minutes / excluded access-setup minutes | |
| Completed both tasks without source edits? | |
| Guide revisions used | |

## Tasks and observations

1. Follow the README installation/no-key demo sequence using the candidate wheel.
   Record the exit code, summary path, zero replay model calls and commit count.
2. Follow the inventory own-app guide. Discover a draft, describe/review/approve,
   replay a second input, inspect a business outcome, obtain input-bound consent
   and verify the adjustment/retry with the independent commit oracle.
3. Explain where the evidence is, what approval binds to, and what to do after an
   uncertain write. Record confusion even if a command eventually succeeds.

Start timing each task before reading its guide. Pause only for downloads on the
demo task and application-access setup on the capability task; record both raw
elapsed and excluded time. Reading, troubleshooting and facilitator help count.
The capability timer measures the inventory sample, not an arbitrary production app.

## Failed commands and undocumented decisions

Record every failed command with sanitized command text, exit code, symptom,
time lost, guide location, facilitator help and workaround. Write "none observed"
only after the trial. Record every undocumented choice or source edit separately.

## Blockers, fixes and retest

For each issue: severity, tracking reference, fix commit, affected guide step,
retest participant/date and observed result. A source-edit workaround or unresolved
blocker prevents completion. If fixes change the candidate, rerun applicable gates
and affected trial steps, preserving the original observations.

## Outcome

Record completion, adjusted times, whether the 15/60-minute goals were met, and
remaining friction. The facilitator reviews this anonymized record before its
SHA-256 is included in the release manifest. Timing goals are observations, not
installation guarantees or automatic release vetoes.
