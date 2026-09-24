import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { expect, test, vi } from 'vitest'

import { processStartMarker } from './backend-claim'
import { bundleHash, copyBundle, runBundleOffline } from './bundle-offline'
import { normalizeRegistry } from './connection-registry'
import { assertBundleReady, writeConnectionsRegistry } from './connection-registry-store'

test.each(['packaged', 'source'])(
  '%s CLI crash leaves lease; recovery preserves backup; stale CAS and unknown fields refuse without mutation',
  async mode => {
    const built = spawnSync(process.execPath, ['scripts/bundle-offline-cli.mjs'], { encoding: 'utf8' })
    expect(built.status, built.stderr).toBe(0)

    for (const phase of [
      'prepared',
      'staged',
      'old-moved',
      'installed',
      ...(process.env.OFFLINE_TEST_RELEASE ? ['runtime-write'] : []),
      'runtime-published',
      'complete'
    ]) {
      const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'bundle-cli-')))

      try {
        const app = path.join(root, 'Hermes.app'),
          candidate = path.join(root, 'candidate.app')

        for (const [dir, commit] of [
          [app, 'b'.repeat(40)],
          [candidate, 'a'.repeat(40)]
        ]) {
          fs.mkdirSync(path.join(dir, 'Contents/Resources'), { recursive: true })
          fs.chmodSync(dir, 0o700)
          fs.chmodSync(path.join(dir, 'Contents'), 0o710)
          fs.chmodSync(path.join(dir, 'Contents/Resources'), 0o700)
          fs.writeFileSync(path.join(dir, 'Contents/Resources/private'), 'private', { mode: 0o600 })
          fs.symlinkSync('Resources/private', path.join(dir, 'Contents/private-link'))
          fs.lchmodSync(path.join(dir, 'Contents/private-link'), 0o700)
          fs.writeFileSync(
            path.join(dir, 'Contents/Resources/install-stamp.json'),
            JSON.stringify({ commit, dirty: false, source: 'local' })
          )
        }

        writeConnectionsRegistry(path.join(root, 'connections.json'), normalizeRegistry(null))
        fs.writeFileSync(path.join(root, 'backend-ownership.json'), '{"backends":[]}')

        const request = {
          action: 'install',
          userData: root,
          installedApp: app,
          candidateApp: candidate,
          expected: { generation: 0, coordinate: null },
          candidateSha256: bundleHash(candidate),
          oldSha256: bundleHash(app),
          candidateCommit: 'a'.repeat(40),
          oldCommit: 'b'.repeat(40),
          transaction: 'cli-test',
          ...(process.env.OFFLINE_TEST_RELEASE
            ? {
                runtimeCandidate: {
                  agentRoot: `${process.env.OFFLINE_TEST_RELEASE}/source`,
                  python: `${process.env.OFFLINE_TEST_RELEASE}/venv/bin/python`,
                  ...JSON.parse(fs.readFileSync('assets/trusted-runtime-manifests.json', 'utf8'))[0]
                }
              }
            : {})
        }

        const archiveBoundary = path.join(root, 'archive-boundary.mjs')
        fs.writeFileSync(
          archiveBoundary,
          `
          import fs from 'node:fs';
          const crash = process.env.BUNDLE_TEST_CRASH;
          let linked = false;
          for (const method of ['linkSync', 'unlinkSync', 'fsyncSync']) {
            const original = fs[method];
            fs[method] = function (...args) {
              const result = original.apply(this, args);
              if (method === 'linkSync') {
                linked = true;
                if (crash === 'archive-linked') process.exit(86);
              }
              if (method === 'fsyncSync' && fs.fstatSync(args[0]).isFile() && fs.fstatSync(args[0]).nlink === 2) linked = true;
              if (method === 'fsyncSync' && linked && fs.fstatSync(args[0]).isDirectory() && crash === 'archive-durable') process.exit(86);
              if (method === 'unlinkSync' && String(args[0]).endsWith('/bundle-install.json') && crash === 'archive-unlinked') process.exit(86);
              return result;
            };
          }
        `
        )

        const invoke = (value: unknown, crash = '') => {
          const file = path.join(root, 'request.json')
          fs.writeFileSync(file, JSON.stringify(value))

          return spawnSync(
            process.execPath,
            [
              '--import',
              path.resolve('scripts/bundle-offline-boundary.fixture.mjs'),
              '--import',
              archiveBoundary,
              ...(mode === 'source'
                ? ['--import', 'tsx', path.resolve('scripts/bundle-offline.ts')]
                : [path.resolve('dist/bundle-offline.mjs')]),
              file
            ],
            {
              encoding: 'utf8',
              timeout: 20000,
              env: { ...process.env, HOME: root, HERMES_HOME: root, BUNDLE_TEST_CRASH: crash }
            }
          )
        }

        expect(invoke({ ...request, expected: { generation: 9, coordinate: null } }).stderr).toContain('stale')
        expect(invoke({ ...request, ignorePID: 123 }).status).toBe(1)
        expect(invoke({ ...request, candidateSha256: '0'.repeat(64) }).stderr).toContain('identity mismatch')
        const crashed = invoke(request, phase)
        expect(crashed.status, crashed.stderr).toBe(86)
        expect(fs.lstatSync(path.join(root, 'runtime-maintenance.lock')).isSymbolicLink()).toBe(true)
        const registryFile = path.join(root, 'connections.json')
        const published = JSON.parse(fs.readFileSync(registryFile, 'utf8'))

        if (request.runtimeCandidate && ['runtime-write', 'runtime-published', 'complete'].includes(phase)) {
          expect(published.connections[0].generalRuntime).toEqual({
            generation: 1,
            coordinate: request.runtimeCandidate
          })
          const external = structuredClone(published)
          external.connections[0].generalRuntime.generation = 9
          fs.writeFileSync(registryFile, JSON.stringify(external))
          expect(invoke({ ...request, action: 'recover' }).stderr).toContain('stale recovery')
          expect(bundleHash(app)).toBe(request.candidateSha256)
          expect(JSON.parse(fs.readFileSync(registryFile, 'utf8')).connections[0].generalRuntime.generation).toBe(9)
          fs.writeFileSync(registryFile, JSON.stringify(published))
        }

        if (phase === 'complete' && request.runtimeCandidate) {
          const bytes = fs.readFileSync(registryFile, 'utf8')
          const backup = path.join(root, '.Hermes.app.cli-test.backup')
          fs.writeFileSync(path.join(backup, 'corrupt'), 'corrupt')
          const corruptHash = bundleHash(backup)
          expect(invoke({ ...request, action: 'recover' }).stderr).toContain('identity mismatch')
          expect(fs.readFileSync(registryFile, 'utf8')).toBe(bytes)
          expect(bundleHash(backup)).toBe(corruptHash)
          expect(bundleHash(app)).toBe(request.candidateSha256)
          fs.unlinkSync(path.join(backup, 'corrupt'))
          expect(invoke({ ...request, action: 'recover' }, 'signature-refused').stderr).toContain('signature refused')
          expect(fs.readFileSync(registryFile, 'utf8')).toBe(bytes)
          expect(bundleHash(app)).toBe(request.candidateSha256)
          expect(() => assertBundleReady(root)).not.toThrow()
          expect(invoke({ ...request, action: 'recover' }, 'restore-refused').stderr).toContain('restore refused')
          expect(JSON.parse(fs.readFileSync(registryFile, 'utf8')).connections[0].generalRuntime).toEqual({
            generation: 2,
            coordinate: null
          })
          expect(fs.existsSync(path.join(root, 'runtime-maintenance.lock'))).toBe(false)
          expect(() => assertBundleReady(root)).toThrow(/bundle/i)
          const mixed = fs.readFileSync(registryFile, 'utf8')
          expect(() => writeConnectionsRegistry(registryFile, normalizeRegistry(null))).toThrow(/bundle/i)
          expect(fs.readFileSync(registryFile, 'utf8')).toBe(mixed)
        }

        const recovered = invoke({ ...request, action: 'recover' })
        expect(recovered.status, recovered.stderr).toBe(0)
        expect(bundleHash(app)).toBe(request.oldSha256)

        if (request.runtimeCandidate && ['runtime-write', 'runtime-published', 'complete'].includes(phase)) {
          expect(JSON.parse(fs.readFileSync(registryFile, 'utf8')).connections[0].generalRuntime).toEqual({
            generation: 2,
            coordinate: null
          })
          expect(bundleHash(path.join(root, '.Hermes.app.cli-test.displaced'))).toBe(request.candidateSha256)
        }

        if (['old-moved', 'installed'].includes(phase)) {
          expect(bundleHash(path.join(root, '.Hermes.app.cli-test.backup'))).toBe(request.oldSha256)
        }

        expect(invoke({ ...request, action: 'recover' }).status).toBe(0)
        expect(() => assertBundleReady(root)).not.toThrow()
        const journal = path.join(root, 'bundle-install.json')
        const original = fs.readFileSync(journal)
        const registry = fs.readFileSync(registryFile)
        const archiveRequest = { ...request, action: 'archive-recovered' }

        if (request.runtimeCandidate) {
          const invalid = JSON.parse(registry.toString())

          for (const generation of [1, 2]) {
            invalid.connections[0].generalRuntime = { generation, coordinate: request.runtimeCandidate }
            fs.writeFileSync(registryFile, JSON.stringify(invalid))
            expect(invoke(archiveRequest).stderr).toContain('stale recovered runtime')
            expect(fs.readFileSync(journal)).toEqual(original)
          }

          fs.writeFileSync(registryFile, registry)
        }

        // Exercise raw publication windows, including a dead shared lease on retry.
        for (const crash of ['archive-linked', 'archive-durable', 'archive-unlinked']) {
          const interrupted = invoke(archiveRequest, crash)
          expect(interrupted.status, interrupted.stderr).toBe(86)
          expect(fs.lstatSync(path.join(root, 'runtime-maintenance.lock')).isSymbolicLink()).toBe(true)
        }

        const archived = invoke(archiveRequest)
        expect(archived.status, archived.stderr).toBe(0)
        const receipt = JSON.parse(archived.stdout)
        expect(receipt.phase).toBe('archived-recovered')
        expect(path.dirname(receipt.archive)).toBe(root)
        expect(fs.readFileSync(receipt.archive)).toEqual(original)
        expect(fs.existsSync(journal)).toBe(false)
        expect(invoke(archiveRequest).status).toBe(0)
        expect(fs.readFileSync(registryFile)).toEqual(registry)
        expect(bundleHash(app)).toBe(request.oldSha256)
      } finally {
        fs.rmSync(root, { recursive: true, force: true })
      }
    }
  },
  60000
)

test('copy preserves restrictive modes without following even external or dangling links', () => {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'bundle-modes-')))

  try {
    const source = path.join(root, 'source'),
      destination = path.join(root, 'copy')

    fs.mkdirSync(source, { mode: 0o700 })
    fs.mkdirSync(path.join(source, 'private'), { mode: 0o700 })
    const outside = path.join(root, 'outside')
    fs.writeFileSync(outside, 'untouched', { mode: 0o600 })
    fs.writeFileSync(path.join(source, 'private/file'), 'private', { mode: 0o640 })

    for (const [name, target] of [
      ['external', outside],
      ['dangling', 'missing'],
      ['internal', 'private/file']
    ]) {
      fs.symlinkSync(target, path.join(source, name))
      fs.lchmodSync(path.join(source, name), 0o700)
    }

    copyBundle(source, destination)

    for (const name of ['', 'private', 'private/file', 'external', 'dangling', 'internal']) {
      expect(fs.lstatSync(path.join(destination, name)).mode).toBe(fs.lstatSync(path.join(source, name)).mode)
    }

    expect(fs.lstatSync(outside).mode & 0o777).toBe(0o600)
    expect(fs.readFileSync(outside, 'utf8')).toBe('untouched')
    expect(fs.readlinkSync(path.join(destination, 'external'))).toBe(outside)
    expect(() => bundleHash(destination)).toThrow('Escaping bundle symlink')
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('unresolved and malformed bundle journals fence native writes without a lease', () => {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'bundle-fence-')))

  try {
    const file = path.join(root, 'connections.json')
    writeConnectionsRegistry(file, normalizeRegistry(null))
    const bytes = fs.readFileSync(file, 'utf8')

    for (const journal of ['{', JSON.stringify({ version: 1, phase: 'restoring' })]) {
      fs.writeFileSync(path.join(root, 'bundle-install.json'), journal)
      expect(() => writeConnectionsRegistry(file, normalizeRegistry(null))).toThrow(/bundle/i)
      expect(fs.readFileSync(file, 'utf8')).toBe(bytes)
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('real bundle swaps preserve backup and recover every interrupted phase without touching registry', async () => {
  for (const crash of ['', 'prepared', 'staged', 'old-moved', 'installed', 'runtime-failure', 'partial-restore']) {
    const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'bundle-offline-')))

    try {
      const app = path.join(root, 'Hermes.app'),
        candidate = path.join(root, 'candidate.app')

      for (const [dir, value] of [
        [app, 'old'],
        [candidate, 'new']
      ]) {
        fs.mkdirSync(dir)
        fs.writeFileSync(path.join(dir, 'payload'), value)
      }

      writeConnectionsRegistry(path.join(root, 'connections.json'), normalizeRegistry(null))
      fs.writeFileSync(path.join(root, 'backend-ownership.json'), '{"backends":[]}')

      const request = {
        action: 'install' as const,
        userData: root,
        installedApp: app,
        candidateApp: candidate,
        expected: { generation: 0, coordinate: null },
        candidateSha256: bundleHash(candidate),
        oldSha256: bundleHash(app),
        candidateCommit: 'a'.repeat(40),
        oldCommit: 'b'.repeat(40),
        transaction: 'test-transaction',
        ...(crash === 'runtime-failure'
          ? {
              runtimeCandidate: {
                agentRoot: root,
                python: '/bin/sh',
                commit: 'a'.repeat(40),
                manifestSha256: '0'.repeat(64)
              }
            }
          : {})
      }

      for (const state of ['alive', 'unknown'] as const) {
        await expect(runBundleOffline(request, { inspect: async () => state, verify: () => {} })).rejects.toThrow(
          'alive or unknown'
        )
      }

      await expect(
        runBundleOffline(request, {
          inspect: async () => 'absent',
          verify: () => {
            throw Error('signature refused')
          }
        })
      ).rejects.toThrow('signature refused')
      expect(bundleHash(app)).toBe(request.oldSha256)
      const registry = fs.readFileSync(path.join(root, 'connections.json'), 'utf8')

      const boundary = {
        inspect: async () => 'absent' as const,
        verify: () => {},
        phase: (p: string) => {
          if (p === crash) {
            throw Error('interrupted')
          }
        }
      }

      if (crash === 'runtime-failure') {
        await expect(runBundleOffline(request, boundary)).rejects.toThrow('old bundle restored')
        expect(bundleHash(path.join(root, '.Hermes.app.test-transaction.displaced'))).toBe(request.candidateSha256)
      } else if (crash === 'partial-restore') {
        await runBundleOffline(request, boundary)
        const partial = path.join(root, '.Hermes.app.test-transaction.restore')
        fs.mkdirSync(partial)
        fs.writeFileSync(path.join(partial, 'partial'), 'partial')
        await expect(runBundleOffline({ ...request, action: 'recover' }, boundary)).rejects.toThrow(
          'partial-restore-copy'
        )
        fs.renameSync(partial, `${partial}.evidence`)
      } else if (crash) {
        await expect(runBundleOffline(request, boundary)).rejects.toThrow('interrupted')
      } else {
        await runBundleOffline(request, boundary)
        expect(fs.readFileSync(path.join(app, 'payload'), 'utf8')).toBe('new')
        expect(fs.readFileSync(path.join(root, '.Hermes.app.test-transaction.backup', 'payload'), 'utf8')).toBe('old')
      }

      await runBundleOffline({ ...request, action: 'recover' }, { ...boundary, phase: () => {} })
      await runBundleOffline({ ...request, action: 'recover' }, { ...boundary, phase: () => {} })
      expect(fs.readFileSync(path.join(app, 'payload'), 'utf8')).toBe('old')
      expect(fs.readFileSync(path.join(root, 'connections.json'), 'utf8')).toBe(registry)
    } finally {
      fs.rmSync(root, { recursive: true, force: true })
    }
  }
})

async function recoveredFixture() {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'bundle-archive-')))
  const app = path.join(root, 'Hermes.app')
  const candidate = path.join(root, 'candidate.app')

  for (const [dir, payload] of [
    [app, 'old'],
    [candidate, 'candidate']
  ]) {
    fs.mkdirSync(dir)
    fs.writeFileSync(path.join(dir, 'payload'), payload)
  }

  writeConnectionsRegistry(path.join(root, 'connections.json'), normalizeRegistry(null))
  fs.writeFileSync(path.join(root, 'backend-ownership.json'), '{"backends":[]}')

  const request = {
    action: 'install' as const,
    userData: root,
    installedApp: app,
    candidateApp: candidate,
    expected: { generation: 0, coordinate: null },
    candidateSha256: bundleHash(candidate),
    oldSha256: bundleHash(app),
    candidateCommit: 'a'.repeat(40),
    oldCommit: 'b'.repeat(40),
    transaction: 'archive-fixture'
  }

  const boundary = { inspect: async () => 'absent' as const, verify: () => {} }
  await runBundleOffline(request, boundary)
  await runBundleOffline({ ...request, action: 'recover' }, boundary)
  const journal = path.join(root, 'bundle-install.json')
  // Whitespace is evidence too: archive must retain bytes, not reserialize JSON.
  fs.writeFileSync(journal, `${JSON.stringify(JSON.parse(fs.readFileSync(journal, 'utf8')), null, 2)}\n`)
  const digest = createHash('sha256').update(JSON.stringify(request)).digest('hex')
  const archive = path.join(root, `bundle-install.${request.transaction}.${digest}.recovered.json`)

  return { root, app, request, boundary, journal, archive }
}

function filesystemEvidence(root: string): unknown {
  const stat = fs.lstatSync(root)

  if (stat.isSymbolicLink()) {
    return { link: fs.readlinkSync(root) }
  }

  return stat.isDirectory()
    ? Object.fromEntries(
        fs
          .readdirSync(root)
          .sort()
          .map(name => [name, filesystemEvidence(path.join(root, name))])
      )
    : fs.readFileSync(root).toString('base64')
}

test('archive-recovered refuses unsafe state without changing any evidence', async () => {
  const fixture = await recoveredFixture()
  const { root, app, request, boundary, journal, archive } = fixture
  const archiveRequest = { ...request, action: 'archive-recovered' as const }
  const original = fs.readFileSync(journal)

  const refuse = async (operation: () => Promise<unknown>, reason: RegExp) => {
    const before = filesystemEvidence(root)
    await expect(operation()).rejects.toThrow(reason)
    expect(filesystemEvidence(root)).toEqual(before)
  }

  try {
    await refuse(() => runBundleOffline(request, boundary), /Previous bundle transaction/)

    for (const state of ['alive', 'unknown'] as const) {
      await refuse(
        () => runBundleOffline(archiveRequest, { ...boundary, inspect: async () => state }),
        /alive or unknown/
      )
    }

    const lock = path.join(root, 'runtime-maintenance.lock')
    fs.symlinkSync(JSON.stringify({ pid: process.pid, marker: await processStartMarker(process.pid, 5000) }), lock)
    await refuse(() => runBundleOffline(archiveRequest, boundary), /Maintenance owner alive or unknown/)
    fs.unlinkSync(lock)

    for (const mismatch of [
      { transaction: 'foreign' },
      { oldSha256: '0'.repeat(64) },
      { candidateSha256: '0'.repeat(64) },
      { oldCommit: '0'.repeat(40) },
      { candidateCommit: '0'.repeat(40) },
      { expected: { generation: 1, coordinate: null } },
      { candidateApp: app },
      { ignorePID: 123 }
    ]) {
      await refuse(
        () => runBundleOffline({ ...archiveRequest, ...mismatch }, boundary),
        /recovered bundle journal|Overlapping paths|Invalid bundle request/
      )
    }

    for (const phase of [
      'prepared',
      'staged',
      'old-moved',
      'installed',
      'runtime-published',
      'complete',
      'restoring'
    ]) {
      fs.writeFileSync(journal, JSON.stringify({ ...JSON.parse(original.toString()), phase }))
      await refuse(() => runBundleOffline(archiveRequest, boundary), /recovered bundle journal/)
    }

    for (const corrupt of ['{', 'null', JSON.stringify({ ...JSON.parse(original.toString()), extra: true })]) {
      fs.writeFileSync(journal, corrupt)
      await refuse(() => runBundleOffline(archiveRequest, boundary), /JSON|recovered bundle journal/)
    }

    fs.writeFileSync(journal, original)
    fs.renameSync(journal, `${journal}.evidence`)
    fs.symlinkSync(`${journal}.evidence`, journal)
    await refuse(() => runBundleOffline(archiveRequest, boundary), /Unsafe/)
    fs.unlinkSync(journal)
    fs.renameSync(`${journal}.evidence`, journal)

    fs.writeFileSync(path.join(app, 'corrupt'), 'tampered')
    await refuse(() => runBundleOffline(archiveRequest, boundary), /identity mismatch/)
    fs.unlinkSync(path.join(app, 'corrupt'))
    await refuse(
      () =>
        runBundleOffline(archiveRequest, {
          ...boundary,
          verify: () => {
            throw Error('signature refused')
          }
        }),
      /signature refused/
    )

    const registry = path.join(root, 'connections.json')
    const registryBytes = fs.readFileSync(registry)
    const stale = JSON.parse(registryBytes.toString())
    stale.connections[0].generalRuntime = { generation: 1, coordinate: null }
    fs.writeFileSync(registry, JSON.stringify(stale))
    await refuse(() => runBundleOffline(archiveRequest, boundary), /stale recovered runtime/)
    stale.connections[0].generalRuntime = {
      generation: 0,
      coordinate: { agentRoot: root, python: '/bin/sh', commit: 'a'.repeat(40), manifestSha256: '0'.repeat(64) }
    }
    fs.writeFileSync(registry, JSON.stringify(stale))
    await refuse(() => runBundleOffline(archiveRequest, boundary), /stale recovered runtime/)
    fs.writeFileSync(registry, registryBytes)

    for (const name of ['runtime-transition.json', 'runtime-rollback.json']) {
      const runtimeJournal = path.join(root, name)
      fs.writeFileSync(runtimeJournal, '{}')
      await refuse(() => runBundleOffline(archiveRequest, boundary), /Unresolved runtime journal/)
      fs.unlinkSync(runtimeJournal)
      fs.symlinkSync(path.join(root, 'missing'), runtimeJournal)
      await refuse(() => runBundleOffline(archiveRequest, boundary), /Unsafe maintenance/)
      fs.unlinkSync(runtimeJournal)
    }

    for (const bytes of [Buffer.from('foreign'), original]) {
      fs.writeFileSync(archive, bytes)
      await refuse(() => runBundleOffline(archiveRequest, boundary), /Occupied archive/)
      fs.unlinkSync(archive)
    }

    fs.linkSync(journal, `${journal}.unexpected-link`)
    await refuse(() => runBundleOffline(archiveRequest, boundary), /Occupied archive/)
    fs.unlinkSync(`${journal}.unexpected-link`)

    fs.mkdirSync(archive)
    await refuse(() => runBundleOffline(archiveRequest, boundary), /Unsafe/)
    fs.rmdirSync(archive)
    fs.symlinkSync(path.join(root, 'missing'), archive)
    await refuse(() => runBundleOffline(archiveRequest, boundary), /Unsafe/)
    fs.unlinkSync(archive)

    const receipt = await runBundleOffline(archiveRequest, boundary)
    expect(receipt).toEqual({ phase: 'archived-recovered', archive })
    expect(fs.readFileSync(archive)).toEqual(original)
    expect(fs.existsSync(journal)).toBe(false)
    expect(bundleHash(path.join(root, '.Hermes.app.archive-fixture.backup'))).toBe(request.oldSha256)
    expect(bundleHash(path.join(root, '.Hermes.app.archive-fixture.displaced'))).toBe(request.candidateSha256)
    const archivedState = filesystemEvidence(root)
    await runBundleOffline(archiveRequest, boundary)
    expect(filesystemEvidence(root)).toEqual(archivedState)

    // Completed repeats still revalidate the retained journal, not just its name.
    fs.writeFileSync(archive, '{')
    await refuse(() => runBundleOffline(archiveRequest, boundary), /JSON/)
    fs.writeFileSync(archive, original)
    fs.renameSync(archive, `${archive}.evidence`)
    fs.symlinkSync(`${archive}.evidence`, archive)
    await refuse(() => runBundleOffline(archiveRequest, boundary), /Unsafe/)
    fs.unlinkSync(archive)
    fs.renameSync(`${archive}.evidence`, archive)

    // A new transaction is possible without erasing any old evidence.
    await runBundleOffline({ ...request, transaction: 'next-install' }, boundary)
    expect(fs.readFileSync(archive)).toEqual(original)
    expect(bundleHash(app)).toBe(request.candidateSha256)
    await refuse(() => runBundleOffline(archiveRequest, boundary), /recovered bundle journal/)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test.each([
  'archive-linked',
  'archive-durable',
  'archive-unlinked',
  'fsync-failure',
  'absence-changed',
  'archive-removed'
])('archive retains exact bytes across %s and retries safely', async crash => {
  const { root, request, boundary, journal, archive } = await recoveredFixture()
  const archiveRequest = { ...request, action: 'archive-recovered' as const }
  const bytes = fs.readFileSync(journal)
  let changed = false

  try {
    await expect(
      runBundleOffline(archiveRequest, {
        ...boundary,
        inspect: async () => (changed ? 'unknown' : 'absent'),
        phase: phase => {
          if (phase === crash) {
            throw Error('interrupted')
          }

          if (phase === 'archive-linked' && crash === 'fsync-failure') {
            vi.spyOn(fs, 'fsyncSync').mockImplementationOnce(() => {
              throw Error('fsync failed')
            })
          }

          if (phase === 'archive-durable' && crash === 'absence-changed') {
            changed = true
          }

          if (phase === 'archive-durable' && crash === 'archive-removed') {
            fs.unlinkSync(archive)
          }
        }
      })
    ).rejects.toThrow(/interrupted|fsync failed|alive or unknown|Occupied archive/)
    expect(fs.readFileSync(fs.existsSync(archive) ? archive : journal)).toEqual(bytes)
    expect(fs.existsSync(journal)).toBe(crash !== 'archive-unlinked')
    await runBundleOffline(archiveRequest, boundary)
    await runBundleOffline(archiveRequest, boundary)
    expect(fs.readFileSync(archive)).toEqual(bytes)
    expect(fs.existsSync(journal)).toBe(false)
  } finally {
    vi.restoreAllMocks()
    fs.rmSync(root, { recursive: true, force: true })
  }
})
