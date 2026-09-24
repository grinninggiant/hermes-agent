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

## Explicit archival after recovery

`install` still refuses **any** existing `bundle-install.json`; it never archives
one automatically. To retire an already recovered transaction, submit the original
install request changing **only** `action` to `archive-recovered`. Keep all original
binding fields, including `expected`, `runtimeCandidate`, transaction, paths,
hashes and commits; do not substitute the current generation or a new candidate.
The original candidate path must still be canonical and present. This operation
never reruns recovery, publishes a runtime, or moves/deletes any app, backup,
stage, displaced bundle or other evidence.

Under the same maintenance lease and real closed-Desktop/backend absence guard,
archival requires:

- The exact original binding and a valid version-1 **recovered** journal only.
  Complete, unresolved, foreign, corrupt and redirected journals are refused.
- The installed bundle's full original hash and original commit, clean build
  stamp, bundle identifier and strict/deep signature verification.
- The original runtime selection, or (when a runtime candidate was bound) the
  original coordinate at `expected.generation + 2`. Recovery that never published
  its target legitimately remains at the original generation, including zero.
  The candidate selection and unrelated newer generations are not accepted.
- No runtime transition or rollback journal, even an apparently owned one.

The returned `archive` is a deterministic file in the same userData root:
`bundle-install.<transaction>.<sha256-of-original-binding-JSON>.recovered.json`.
It retains the **exact original journal bytes**, not a rewritten record. An
exclusive hard link publishes these bytes without overwriting an existing name;
the file and containing directory are synced before unlinking the active journal,
and the directory is synced again afterward. No evidence is pruned.

After interruption, rerun the identical archive request. If both names exist,
only the same inode and exact bytes (the interrupted hard-link publication) can
resume. A different file is an occupied destination **even with identical bytes**;
symlinks, directories and unexpected hard links fail closed. If only the retained
archive exists, retry verifies it and all live guards again before returning
success. A missing archive never authorizes removing the active journal. Filesystems
that refuse hard links/fsync fail closed; there is no copy/overwrite fallback.
These are cooperative maintenance guarantees, not protection against a hostile
process rewriting files outside the lease or storage ignoring fsync.

A subsequent install is a separate explicitly authorized request with a **new
transaction ID** and its own current expected selection. Old backup/displaced
paths and archived journals remain retained. Archival is not permission to bypass
an alive/unknown Desktop, ownership checks, trust pins, or release approval.

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
Archival tests verify original-generation and rolled-back-generation recovery,
exact retained bytes, independent-file collision refusal, phase/binding/runtime/
bundle/signature/absence guards, and new-transaction installation after archival.
Both source and packaged CLI subprocesses exit abruptly after linking, directory
fsync and unlinking; retries reclaim dead leases and retain the original evidence.
In-process tests additionally inject fsync failure and changing absence/evidence.
These are not live installed-app acceptance or signed candidate verification.
