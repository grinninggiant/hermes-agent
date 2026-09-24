# Scoped General local runtime — reviewed source slice, not activation

## Current upstream port status

The Desktop-only port is based on upstream `f97608f178d1ffeca59860195ab7da295f7c8e5f`.
The trust manifest remains byte-identical to the installed Desktop source; it
still authorizes only the reviewed `be1410537315235c821150789689a7a198ccb512`
artifact below. Updating Desktop source does not approve a new runtime manifest.

Upstream now attaches to a host backend before resolving a local runtime and
shares local profile routes, including REST paths. An exact General selection
must bypass that attachment and must not share another profile's runtime in
either direction. Start-ticket generation/recovery checks fence the attachment
as well as process spawn; unrelated remote routes keep their existing behavior.
The async runtime resolver, lifecycle cancellation, owned-child spawn, recovery
promise ordering and guest preload from upstream are retained.

The standalone upstream core lacks `tui_gateway.owner_bootstrap`. The real Python
owner-channel integration test intentionally remains failing until the companion
core changes are integrated and verified; it is not skipped or replaced by a
fixture. A new runtime additionally requires explicit trust-pin approval. The
historical verification receipts below describe the original source slice, not
acceptance of this port. No deployment or live runtime selection is performed.

`RegistryConnection.generalRuntime` retains an explicit coordinate or null plus a
monotonic generation. Label saves retain it and cannot supply an executable.
Primary and pooled local serve resolution consume it only for `general`; shared
runtime constants, remote authentication, CLI helpers and gateways are unchanged.

## Reviewed artifact trust

The schema-2 builder contract is validated without running candidate Python:
exact target/source/final venv Python/commit, manifest SHA256, complete source plus
generated metadata inventory, complete venv inventory (directories, executable
bits, file hashes and interpreter links), owner/non-writable seal, and resolved
base interpreter identity and hash. Unexpected entries and symlinks fail closed.
This is artifact identity, not connection authentication or protection against
same-user mutation between validation and spawn.

`assets/trusted-runtime-manifests.json` is provisioned through the existing
`package.json` `build.extraResources` mechanism, loaded from
`process.resourcesPath/trusted-runtime-manifests.json`. Both commit and digest
must match. It contains no installation path or credentials. Reviewed candidate:

- Commit: `be1410537315235c821150789689a7a198ccb512`
- Manifest: `75b0d1ff27f9a8d1f186c49bf3cfbd6852a052c67453168e6348aabe0bee48cc`

The actual builder artifact passed this native validator, including the entire
source/generated/venv inventories and external interpreter digest. The pin is
approval of those bytes only; it does not select a runtime or establish that this
candidate contains the new process-drain protocol.

## Fail-closed persistence

Malformed runtime selections cannot enter the registry's best-effort quarantine
and become an unset local runtime. Authoritative disk reads reject invalid JSON,
invalid registry shape, ambiguous/missing local entries and quarantined runtime
selections. Main leaves disk bytes and the previous cache/coordinate untouched
and throws before drift reconciliation or spawning. Missing files after an
in-memory registry has been loaded also fail closed. First-run migration remains
available only when no registry or previous cache exists.

## Closed-app maintenance entry (source-only, not deployed)

From `apps/desktop`, run:

```sh
node --import tsx scripts/runtime-offline.ts /absolute/path/request.json
```

The request is `{ "action": "activate", "userData": "/absolute/Desktop/userData",
"installedApp": "/Applications/Hermes.app", "expected": { "generation": 0,
"coordinate": null }, "candidate": { "agentRoot": "/absolute/release/source",
"python": "/absolute/release/venv/bin/python", "commit": "be1410537315235c821150789689a7a198ccb512",
"manifestSha256": "75b0d1ff27f9a8d1f186c49bf3cfbd6852a052c67453168e6348aabe0bee48cc" } }`.
Paths and expected selection must be reviewed for the actual installation; these
are schema examples, not an instruction to manufacture a generation or path.
`rollback` and `recover` use the same request shape without candidate, and require
the exact current selection in `expected`. Only the source-provisioned pins are
accepted; the CLI has no trust override and starts neither Electron nor backend.

Quit Desktop manually and keep it closed until the command completes. macOS only.
The entry independently scans processes, probes all recorded Desktop backend identities
and recorded parents, and rejects alive/unknown/missing/malformed ownership. It
also refuses recognizable serve/dashboard processes unless the exact independent
General SDK launchd service is proven below; it never stops one. Process inspection uses existing ownership
parsing, command matching and start-marker contracts. It does not read credentials
or decrypt connection envelopes. No default personal userData path is inferred.

The controller now has a closed-app transition, persistent fsynced recovery journal,
CAS, and native start tickets. The shared native registry writer is reused; no
raw registry patch or global runtime pointer is introduced. Successful activation
verifies exact selection readback, retains `runtime-rollback.json`, then clears
`runtime-transition.json` so manual reopen can resolve the selection. An interrupted
publication leaves the pending journal blocking General starts; explicit recovery
restores the prior coordinate (including null) only at its owned generation.
Generations remain monotonic: restoring unset means a null coordinate, not deleting
the generation. A retained rollback receipt blocks another activation.

A process-identity maintenance lease excludes concurrent maintenance commands;
confirmed dead owners can be reclaimed. Unknown owners fail closed. This source
also blocks native registry writes/General start tickets while the lease exists.
An older installed app does not know that fence: **do not reopen during maintenance**.
This is not a distributed transaction or protection against same-user file changes,
launch races with old builds, or mutable release contents after validation.

Automatic live transitions remain unavailable: authenticated process idle alone
cannot authorize termination, known serve producers are not all drained, and no
native graceful-exit/readiness acceptance protocol is exposed. No activation IPC
is exposed. Packaging, installed-app acceptance, manual quit/reopen, and live
primary/pool identity verification remain separate parent-reviewed human gates.
No active installation, personal connection registry, auth policy, other profiles,
or running app was modified during implementation/testing.

## Independent CLI review and execution

The executable CLI was exercised on macOS with Node v25.9.0 against the real
production-pinned v5 artifact (read-only), with a fresh temporary registry and
HOME/HERMES_HOME for every test. Reproduce from `apps/desktop`:

```sh
OFFLINE_TEST_RELEASE=/absolute/path/to/sealed-v5-release npx vitest run --config vitest.runtime.config.ts electron/runtime-offline-cli.test.ts
```

The subprocess runs `node --import scripts/runtime-offline-boundary.fixture.mjs
--import tsx scripts/runtime-offline.ts REQUEST.json` (the preload path is resolved
absolute by the test). This **test-only preload** substitutes only the exact
`/bin/ps -axo pid=,command=` inventory; PID/start-marker lease probes remain real.
It is not a production inventory bypass or a request field. Production uses the
unmodified OS inventory. The crash injection kills only its own isolated test
CLI process, immediately after registry rename.

Readbacks: activation generation 1/pinned coordinate; stale rollback refused;
rollback generation 2/null; actual SIGKILL left generation 3/pinned coordinate,
pending journal and lease; a fresh CLI reclaimed the dead lease and recovered to
4/null, clearing journal and lease. Request trust fields, executable substitution,
noncanonical candidate paths, wrong manifest pins and metadata symlink redirects
were refused. The writer regression first demonstrated rename-before-fsync;
the shared writer now fsyncs an exclusive unique temporary file before rename,
then fsyncs the directory. All native registry writers use this shared primitive;
runtime CAS remains the shared exact generation/coordinate transaction, **not**
a cross-process filesystem CAS for arbitrary writers or old app builds.

Focused review run: **153 tests passed across 8 files**; full Desktop renderer,
Electron and E2E typechecks passed; `git diff --check` passed. No live personal
registry, app installation, process or credentials were modified.

Independent SDK ownership is narrowly recognized for the current user's existing
`gui/<uid>/ai.hermes.serve-general` job: exact LaunchAgent path, wrapper program,
running job PID and run count, exact General argv, and the interpreter from the
existing `.local/bin/hermes` launcher must agree. Job and process start/executable
readbacks must remain stable; every inspection repeats them. Missing, duplicate,
stale, conflicting or timed-out evidence leaves serve blocking. Installed-app and
all Desktop ownership checks run first; even stale same-PID Desktop records block
the exemption. No request field can choose an ignored PID, service or port.
This is process ownership classification, not new executable authorization or a
same-user tamper defense. It retains the existing macOS second-resolution start
identity limitation and the operator-reviewed service contract. A port number or
missing ownership row alone never proves independence.
Do not kill or ignore that service to force passage. Installed-app path and
userData remain operator-reviewed inputs, not automatically attested discovery.
Repeated inventory checks and the cooperative maintenance lease cannot prevent
an old app launching between the final check and publication. Keeping it closed,
packaging/install approval, and subsequent primary/pool live identity acceptance
remain human bootstrap gates; these tests do not close those gates.

## Verification

Node 22.22.0; frozen root `npm ci --ignore-scripts --no-audit --no-fund`, then the
locked Electron development dependency's install script. No lockfile changes.

- RED: schema-2 fixture rejected; malformed runtime silently normalized to default.
- GREEN: focused runtime/registry/primary/backend-state suite.
- Full `npm run typecheck --workspace apps/desktop`: renderer, Electron and E2E pass.
- Full native suite: 2064 passed, 6 skipped, 1 unrelated failure in the existing
  POSIX managed SSH launcher test (Linux command run on macOS exits 127).
- `git diff --check`: pass.

Tests prove validation, persistence, scope and packaging configuration, not a
running Electron deployment or safe production activation.
