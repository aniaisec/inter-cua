# Your web application

This is an editable, conservative starting point with no approved capabilities.
Before any discovery:

1. Replace `https://replace-me.invalid` in `tenants/local.yaml` with your app URL.
2. Copy `.env.example` to `.env`, set your own login and provider key locally.
3. Edit `policies/default.yaml`: enumerate exact application paths and credential
   sinks. Its initial button rule requires consent for every button; replace it
   only after classifying each control and identifying every committing action.
4. Edit `capabilities/families/my-web-app.yaml`: name the vendor/version and add
   tested business rejections, session expiry, and recovery rules. The empty
   detectors are placeholders, not evidence that the app has no failure states.

Your next command, after those edits (replace the goal and output with your app):

```sh
python -m playwright install chromium
cua discover --goal "Look up an item" --name lookup --entry /login --output result:string
cua describe lookup
cua approve lookup --by YOUR_NAME
cua replay lookup
```

Use a test account and synthetic input. A live provider needs your own key;
discovery with `--llm scripted --script YOUR_SCRIPT.yaml` needs none. Malformed
family data fails recording preflight before model work or a UI launch. A valid
but incomplete template still needs application-specific review and negative
tests: one successful discovery cannot infer all business failures.

Replay requires explicit review/approval. Never grant broad paths or mark
committing controls safe simply to make discovery succeed. Keep `.env` and the
unique project signing key in `.cua/` private. No API clients are created by this
template; configure them deliberately if you later expose a service.
