# Isolated startup smoke (not production acceptance)

From `apps/desktop`, with workspace dependencies, a supported Node version, and an
existing `dist/` renderer plus staged native dependencies:

```sh
node scripts/startup-smoke.mjs
```

The runner copies the existing renderer/native artifacts into a fresh temporary
app, rebuilds main and preload, and replaces `startup-effects.ts` via an esbuild
resolver. The production build script never selects this replacement. No test
environment variable can disable production authority. The copied renderer is
not rebuilt by this focused native-startup test.

## Audited cold-start scope

- Main startup and renderer connection calls go through `startBackend`, before
  backend resolution, bootstrap, spawn, ownership claims, or orphan reaping.
- Reaping and the owned-child/process-tree stop entry points have separate seams.
  Existing identity validation and shutdown coordination remain in production.
- Login-shell warmup, OS URL registration, global quick-entry registration,
  managed SSH recovery, and passive update checks delegate normally in production;
  the test replacement never invokes their supplied callbacks.
- Existing media permission, protocol, header and window security setup remains
  active. The fixture does not supply connection/provider settings or credentials.
- The child receives only the explicit environment in `launch.json`. HOME,
  HERMES_HOME, user-data, Electron home/appData/sessionData/temp/logs/crashDumps
  point into the fresh temporary tree. The process starts with an explicit
  `--user-data-dir`; no sandbox-disabling argument or entitlement change is used.
- Test-entry tripwires reject Node child-process creation, process signals,
  Node network calls, OS protocol registration, and global shortcut registration.
  Chromium HTTP(S)/WebSocket requests are cancelled, including the renderer's
  external font request. These are test-only additional restrictions, not a new
  production security policy or a claim of whole-OS filesystem containment.
- No interaction with repair/install/retry controls is attempted. This does not
  exercise arbitrary IPC, backend operation, remote connections or updates.

## Acceptance and evidence

The real Electron main must become ready, create the production window with
`sandbox=true`, `contextIsolation=true`, `nodeIntegration=false`, and mount the
renderer DOM without Node globals. Because the backend is deliberately blocked,
the expected visible UI is Hermes's startup-recovery screen, not a usable chat.
The test entry captures `renderer.png`, then calls `app.quit()`; its 30-second
fallback also only calls `app.quit()`. No process sweep or forced kill is used.
The runner records the actual child exit and fails if the quit receipt is absent,
if a forbidden effect was attempted, or if DOM/security checks failed.

Each run prints its temporary artifact directory containing `trace.jsonl`,
`electron.log`, `exit.json`, `launch.json`, `metafile.json`, `renderer.png` and
`app/`. Missing install-stamp warnings and backend-unavailable IPC errors are
expected for this disposable test bundle. None of these receipts certify the
signed candidate, installed app, production backend, or update/activation path.
