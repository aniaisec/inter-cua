# Manual discovery, review and replay

After [setup](../README.md#installation-and-no-key-demo), activate the environment
and configure the synthetic mock credential as in [getting started](getting-started.md).
This walkthrough keeps the services running while you inspect each stage yourself.

Start the mock app in one terminal and leave it running:

```bash
make mockapp                       # or: cua mockapp   -> http://127.0.0.1:8000 (sign on operator / operator)
```

Everything else runs in a second terminal with the venv activated.

**1. Discover.** A model drives the app from the goal alone and the run is
recorded as a draft capability. `--capabilities-dir demo` keeps it out of the
committed `capabilities/`.

```bash
cua discover --goal "Look up a member by id and return the current savings balance" \
  --name member_savings_balance --entry /login \
  --param member_id:string=10003 \
  --output savings_balance:decimal --output "member_name:string?" \
  --capabilities-dir demo
```

It prints the run directory (`evidence/runs/<run_id>/`: the model's reason for
every action in `log.jsonl`, response ids and token counts in
`model_calls.jsonl`, masked screenshots) and `demo/member_savings_balance.json`.

No key? Add `--llm scripted --script scripts/discovery/member_savings_balance.yaml`
to the same command. The same loop, policy checks and recorder run; the
"model" plays back a recorded tool-call sequence.

**2. Review and approve.** Replay refuses a draft. `describe` renders the
capability for a person who is not an engineer; `approve` refuses unless the
capability is exactly what `describe` last showed.

```bash
cua describe demo/member_savings_balance.json
cua approve demo/member_savings_balance.json --by reviewer
```

**3. Replay.** No model is involved from here on. Each call prints one JSON
`ReplayResult`, and the exit code says which of the four kinds it is.

```bash
cua replay demo/member_savings_balance.json --input member_id=10003                          # 0 success: savings_balance 1411.21
cua replay demo/member_savings_balance.json --input member_id=99999                          # 2 business_outcome: NOT_FOUND at search.submit
cua replay demo/member_savings_balance.json --input member_id=10003 --inject session_expired # 0 success, after a re-login recovery
cua replay demo/member_savings_balance.json --input member_id=10003 --inject server_error    # 1 failure: APP_ERROR, expected/observed, trace.zip
cua replay demo/member_savings_balance.json --input member_id=ten                            # 1 failure: INPUT_INVALID, no browser started
```

**4. Hand a stuck run to a person.** Start the operator console in a third
terminal, then make the Search button read "Find", which the capability has
never seen:

```bash
cua operator                       # http://127.0.0.1:8100
```

```bash
cua replay capabilities/member_savings_balance.json --input member_id=10003 \
  --inject renamed_button --handoff --headed
```

The replay pauses and opens an intervention request. On the console press
**Take control**, click **Find** in the browser window, then **Resume**. The
run checks the screen against its checkpoints, finds the member detail page
already showing the balance (`cp.done`), reads it, and returns `success`
with the handoff in the result (`handoffs[0]`) and your click in the run's
`human_actions.jsonl`.

**5. A step that commits something.** Opening a sub-account ends on an
irreversible Confirm. Without consent the run does not press it; with a
signed, single-use approval token it runs unattended:

```bash
cua replay capabilities/open_subaccount.json --input member_id=10003 --input initial_deposit=250.00 --handoff
```

(pauses at `review.submit` with `NEEDS_APPROVAL`; **Approve action** on the console commits once)

```bash
cua replay capabilities/open_subaccount.json --input member_id=10003 --input initial_deposit=250.00 \
  --approval-token "$(cua approval-token capabilities/open_subaccount.json --input member_id=10003 --input initial_deposit=250.00 --by reviewer)"
```

(commits unattended; `side_effect: committed` and the reference number)

The multiline commands in this walkthrough use POSIX shell syntax. For first-run
PowerShell setup, use the [README commands](../README.md#installation-and-no-key-demo).

[Documentation index](index.md) · [Project README](../README.md)
