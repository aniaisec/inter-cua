"""Measure whether discover-once-then-replay beats asking a model every time.

Two ways of getting the same task done against the same mock app, under the
same policy, credentials and injected failures:

``baseline_llm``         a model operates the UI on every invocation (the
                         discovery loop, run as an agent rather than to
                         record anything);
``inter_cua_discovery``  the one model run that turns the goal into a
                         capability;
``inter_cua_replay``     the approved capability, replayed with no model.

Every invocation is one ``RunMetrics`` line in ``bench/reports/runs.jsonl``;
``cua benchmark report`` aggregates them into ``summary.json`` and
``summary.md``. Tasks live in ``bench/tasks/*.yaml`` and state the ground
truth — what a correct operator would conclude — not what either strategy is
expected to do, so the same scoring applies to both.
"""
