# Synthetic browser project

No provider key is required. All data and credentials belong to the local mock app.
Run these commands from this directory after installing `inter-cua` and Chromium:

```sh
python -m playwright install chromium
cua demo --out demo/session --repetitions 2
```

The seven-stage demo starts its own mock app and console, discovers a draft,
explicitly describes and approves it, replays, demonstrates drift and human
handoff, checks single-use consent, and blocks a hostile navigation. Its scripted
reviewer is a walkthrough, not an approval for your production application.
Read `demo/session/summary.md` and the saved evidence afterward.

To review the starter artifacts yourself, start `cua mockapp` in a second terminal:

```sh
cua describe member_savings_balance
cua approve member_savings_balance --by YOUR_NAME
cua replay member_savings_balance --input member_id=10003
```

All bundled capabilities begin as drafts. Editing a capability invalidates its
approval; review its locators, outcomes, checkpoints, and risky actions again.
The signing key under `.cua/` is unique to this project. Keep `.env`, keys, review
receipts, and runtime evidence private. API access is local-only unless you
deliberately configure and secure a deployment.
