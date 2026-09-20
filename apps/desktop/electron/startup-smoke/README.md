# Isolated startup smoke (not production acceptance)

From `apps/desktop`, with workspace dependencies, a supported Node version, and an
existing `dist/` renderer:

```sh
node --test electron/startup-smoke/guards.test.mjs
node --test electron/startup-smoke/signing.test.mjs # macOS, /usr/bin/python3 required
node scripts/startup-smoke.mjs
# macOS: copy only the dependency Electron.app into the fixture, ad-hoc sign it,
# and verify the copy with codesign --verify --deep --strict before execution.
node scripts/startup-smoke.mjs --signed-copy
```

The runner copies the existing renderer (excluding native modules) into a fresh temporary
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
  Prototype calls (`Socket.connect`, `Server.listen`, `ChildProcess.spawn/kill`),
  PTY named/default imports, Electron utility processes and shell open operations
  are guarded before importing the application. Only the exact Courier Prime GET
  stylesheet URL is classified as an expected request; it is **still cancelled**.
  Any other HTTP(S)/WebSocket request increments the violation counter.
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

The DOM assertion requires a complete, mounted document, the visible exact
`Hermes couldn't start` heading, and exactly the expected `hermes:connection`
backend-unavailable error within that heading's recovery overlay. It does not
mistake the offline badge, hidden SVG icons, or another overlay's errors for the
recovery error.

## Negative Electron probes

```sh
node scripts/startup-smoke.mjs --signed-copy --probe-tripwires
node scripts/startup-smoke.mjs --signed-copy --probe-network
```

Both commands **must exit 1** even though the owned Electron process exits cleanly
with code 0. This is deliberate: negative probes never clear violations or turn
failed startup acceptance into success. Inspect the receipts, not exit 1 alone:

- Tripwires: `tripwire-probes-complete.count=11`, eleven matching
  `tripwire-probe`/`forbidden-effect` pairs, `quit.violations=11`, `passed=false`,
  no timeout/import/probe error. Uses real Node prototypes and Electron APIs;
  PTY is the test-only replacement. Invalid arguments prevent native effects even
  if an individual guard regresses. No application entry is imported in this mode.
- Network: renderer recovery still loads; the exact font stylesheet is expected
  and cancelled, the reserved `https://unexpected.invalid/startup-smoke-probe`
  request is unexpected and cancelled, `quit.violations=1`, `passed=false`.
- Normal run: ready/window/renderer receipts, `quit.violations=0`, `passed=true`,
  runner and Electron exit 0. There is no network allowance in any mode.

`--signed-copy` uses the repository's `_desktop_macos_local_codesign` inside-out
helper with explicit `identity="-"`. The test-only Python adapter isolates that
function and its two inspected dependencies via AST, avoiding CLI import effects,
identity discovery and legacy fallback. It creates a new copy (refusing an existing
target), checks file/symlink inventory equality before signing and source equality
afterward, and compares actual main/all-helper entitlements with the repository
plists after deep strict verification. Set `TMPDIR` to a profile-scoped artifact
directory when retaining these disposable copies there.

The runner records `signature.json`, sorted `source-before.json`, `copy-before.json`,
`source-after.json`, `copy-signed.json` inventories and the exact executable in
`launch.json`. The signature receipt includes inventory hashes, the helper source
hash, declared entitlement hashes, actual entitlements and identifier/DR details.
It is an **ad-hoc signature of a temporary Electron copy**, not Developer ID,
notarization, signing of a production candidate, or real-agent/AC5 acceptance.
It never signs the source dependency or touches `/Applications`. Do not top-level
await `app.whenReady()` in the ESM fixture: Electron waits for entry evaluation
before dispatching readiness; schedule the probe callback instead.
