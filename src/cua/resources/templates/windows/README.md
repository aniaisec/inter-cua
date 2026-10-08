# Windows UI Automation project

This starter includes the synthetic DeskCalc WinForms app. Use an interactive
Windows desktop and install `inter-cua[windows]`. UIA cannot run in a headless
Linux job or a locked/noninteractive Windows session.

Your next command, from this directory:

```powershell
cua doctor --tenant local
cua discover --llm scripted --script scripts/discovery/deskcalc_compute.yaml --goal "Calculate a quotient" --name deskcalc_compute --entry / --param first:decimal=12.5 --param second:decimal=4 --param operation:string=Divide --output result:decimal
cua describe deskcalc_compute
cua approve deskcalc_compute --by YOUR_NAME
cua replay deskcalc_compute --input first=20 --input second=4 --input operation=Divide
```

No provider key or login is needed for this script. For your own app, replace
the `desktop.launch` argv and `uia://` location in `tenants/local.yaml`; arguments
are a list, not a shell command. Replace the policy, family, and script for your
actual controls, business outcomes, and irreversible actions. Record appends to
DeskCalc's ledger and requires explicit consent.

UIA does not provide web frames, HTTP document status, browser dialogs, CDP
handoff, or the vision fallback. Do not add those features to a desktop family.
Run `cua surfaces` to inspect supported features. All new capabilities are drafts
until reviewed and approved. The signing key under `.cua/` is private and unique;
no API access configuration is created by this template.
