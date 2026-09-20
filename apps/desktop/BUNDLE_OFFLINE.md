# Closed-app bundle/runtime coordinator

This CLI publishes the bundle first, then the existing trusted runtime CAS under
**one shared maintenance lease**. It is ordered and recoverable, not atomic across
resources. No launch, quit, signal, service/auth/policy change or backup deletion.
The operator must keep Desktop closed throughout installation and recovery; the
cooperative lease does not prevent an older app or the OS from launching.

## Build and run

From `apps/desktop`:

```sh
node scripts/bundle-offline-cli.mjs
node dist/bundle-offline.mjs /absolute/path/request.json
```

`dist/bundle-offline.mjs` is self-contained (Node required, no tsx/node_modules or
Electron startup). The normal Electron bundler includes both offline CLIs too.
The previously signed candidate does NOT contain these changes; rebuilding and
reviewing a signed candidate remains a separate human-owned release gate.

Request example (replace approval placeholders; this is not a live request):

```json
{
  "action": "install",
  "userData": "/absolute/operator-selected/userData",
  "installedApp": "/absolute/operator-selected/Hermes.app",
  "candidateApp": "/absolute/approved/candidate/Hermes.app",
  "expected": {"generation": 0, "coordinate": null},
  "runtimeCandidate": {
    "agentRoot": "/absolute/existing/sealed-release/source",
    "python": "/absolute/existing/sealed-release/venv/bin/python",
    "commit": "EXISTING_BAKED_PIN_COMMIT",
    "manifestSha256": "EXISTING_BAKED_PIN_MANIFEST_SHA256"
  },
  "candidateSha256": "APPROVED_64_HEX_BUNDLE_HASH",
  "oldSha256": "APPROVED_64_HEX_BUNDLE_HASH",
  "candidateCommit": "APPROVED_40_HEX_COMMIT",
  "oldCommit": "APPROVED_40_HEX_COMMIT",
  "transaction": "unique-operator-transaction-id"
}
```

Omitting `runtimeCandidate` retains bundle-only behavior. The runtime trust pins
are baked from the existing assets file, never supplied/overridden by a request.
Bundle hashes use exported `bundleHash` (sorted names/types/modes/bytes/link targets),
not archive hashes/CDHash. Production verifies complete hashes, clean build stamps,
commit, bundle identifier and codesign strict/deep integrity. Ad-hoc signature
integrity is not publisher identity. No new sealed release or trust pin is created.

## Recovery

Submit the identical request changing only `action` to `recover`; retain the
original `expected`, even after runtime generation advances. The bundle journal
records exact prior runtime selection and intended target, binding all request
identities to phases prepared/staged/old-moved/installed/runtime-published/complete/
restoring/recovered. Files and publication directories are synced.

Recovery runs in reverse order: existing runtime rollback CAS first, bundle second.
Only exact before/target/rolled-back versions and matching runtime journal ownership
are accepted. A newer external selection or foreign journal blocks without being
overwritten. Runtime rollback advances generation; it never restores raw registry
bytes. A recover request also explicitly rolls back a completed installation.
Repeated recovery is idempotent. Pending runtime publication survives abrupt exit
between registry publication and finalization.

Ordinary runtime activation failure automatically restores the old bundle if state
is still owned and safe, returning a failure receipt rather than false success.
Old `.backup` and candidate `.displaced` remain; journals and stage evidence remain.
Partial initial stage copying leaves the old installed bundle recoverable. Partial
restore copies return an actionable JSON blocked receipt with exact retained backup
and partial-copy paths: move the partial copy to a separate retained evidence path,
then retry the identical recover request. Never alter the verified backup. Unknown
bundle identity, occupied destinations or unsafe process evidence remain blocking.

## Verification (temporary fixtures only)

```sh
OFFLINE_TEST_RELEASE=/Users/mutlupolatcan/.hermes/profiles/general/artifacts/ops239-be141053-agent-release-v5 npx vitest run --project electron electron/bundle-offline.test.ts electron/runtime-offline.test.ts electron/runtime-offline-cli.test.ts electron/runtime-independent-service.test.ts
npx tsc -p tsconfig.electron.json --noEmit
npx eslint electron/bundle-offline.ts electron/bundle-offline.test.ts electron/runtime-offline.ts
```

The subprocess test builds and executes the self-contained CLI. It crashes at bundle
journal boundaries, raw runtime registry publication, runtime-published and complete;
recovers dead leases; rejects stale external versions; verifies generation 1 target
then generation 2 prior coordinate; retains both bundles and retries recovery.
The sealed artifact uses the actual existing pin with no synthetic manifest.
Only process inventory/signature/PlistBuddy are fixture boundaries. Additional tests
exercise activation refusal restoration and the partial-copy blocked/retry receipt.
These are not live installed-app acceptance or signed candidate verification.
