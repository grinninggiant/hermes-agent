import { execFileSync } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import pins from '../assets/trusted-runtime-manifests.json' with { type: 'json' }

import { readConnectionsRegistry } from './connection-registry-store'
import { normalizeRuntimeSelection, type RuntimeCoordinate, type RuntimeSelection } from './connection-runtime'
import type { inspectClosedDesktop } from './runtime-offline'
import { runOfflineLocked, withOfflineMaintenance } from './runtime-offline'
import { RuntimeTransitionJournal } from './runtime-transition'

export interface BundleRequest {
  action: 'install' | 'recover' | 'archive-recovered'
  userData: string
  installedApp: string
  candidateApp: string
  expected: RuntimeSelection
  runtimeCandidate?: RuntimeCoordinate
  candidateSha256: string
  oldSha256: string
  candidateCommit: string
  oldCommit: string
  transaction: string
}

/** Content identity includes names, types, modes and symlink targets, not timestamps. */
export function bundleHash(root: string): string {
  const hash = createHash('sha256')

  const walk = (name: string) => {
    const file = path.join(root, name),
      stat = fs.lstatSync(file)

    const kind = stat.isSymbolicLink() ? 'link' : stat.isDirectory() ? 'dir' : stat.isFile() ? 'file' : 'unsafe'

    if (kind === 'unsafe') {
      throw new Error('Unsupported bundle entry')
    }

    hash.update(JSON.stringify([name, kind, stat.mode & 0o777]))

    if (kind === 'link') {
      const target = fs.readlinkSync(file)
      const resolved = path.resolve(path.dirname(file), target)

      if (!resolved.startsWith(`${root}/`)) {
        throw new Error('Escaping bundle symlink')
      }

      hash.update(JSON.stringify(target))
    } else if (kind === 'dir') {
      for (const child of fs.readdirSync(file).sort()) {
        walk(path.join(name, child))
      }
    } else {
      hash.update(JSON.stringify(stat.size))
      hash.update(fs.readFileSync(file))
    }
  }

  walk('')

  return hash.digest('hex')
}

/** Node 22 cpSync widens directory/link modes; restore them without following links. */
export function copyBundle(source: string, destination: string): void {
  fs.cpSync(source, destination, {
    recursive: true,
    dereference: false,
    verbatimSymlinks: true,
    errorOnExist: true,
    force: false
  })

  const modes = (from: string, to: string) => {
    const original = fs.lstatSync(from)
    const copied = fs.lstatSync(to)

    if (original.isSymbolicLink()) {
      if (!copied.isSymbolicLink()) {
        throw new Error('Copied bundle type mismatch')
      }

      fs.lchmodSync(to, original.mode & 0o7777)
    } else {
      if (original.isDirectory()) {
        if (!copied.isDirectory() || copied.isSymbolicLink()) {
          throw new Error('Copied bundle type mismatch')
        }

        for (const child of fs.readdirSync(from)) {
          modes(path.join(from, child), path.join(to, child))
        }
      } else if (!original.isFile() || !copied.isFile() || copied.isSymbolicLink()) {
        throw new Error('Copied bundle type mismatch')
      }

      // Post-order: do not restrict traversal before descendants are repaired.
      fs.chmodSync(to, original.mode & 0o7777)
    }
  }

  modes(source, destination)
}

/** Only structured OS diagnostics / fixed identity failures, never stderr or arbitrary messages. */
function copyFailure(error: unknown) {
  const value = error as NodeJS.ErrnoException & { status?: number }

  return {
    code: typeof value?.code === 'string' && /^[A-Z_0-9]+$/.test(value.code) ? value.code : undefined,
    syscall: typeof value?.syscall === 'string' && /^[a-zA-Z0-9_]+$/.test(value.syscall) ? value.syscall : undefined,
    status: Number.isInteger(value?.status) ? value.status : undefined,
    detail: value?.message === 'Bundle identity mismatch' ? value.message : 'Bundle copy or verification failed'
  }
}

function verifyBundle(app: string, commit: string): void {
  if (process.platform !== 'darwin') {
    throw new Error('macOS required')
  }

  execFileSync('/usr/bin/codesign', ['--verify', '--deep', '--strict', app], { stdio: 'pipe' })
  const stamp = JSON.parse(fs.readFileSync(path.join(app, 'Contents/Resources/install-stamp.json'), 'utf8'))

  if (stamp.commit !== commit || stamp.dirty !== false || stamp.source === 'fallback') {
    throw new Error('Build stamp mismatch')
  }

  const identifier = execFileSync(
    '/usr/libexec/PlistBuddy',
    ['-c', 'Print :CFBundleIdentifier', path.join(app, 'Contents/Info.plist')],
    { encoding: 'utf8' }
  ).trim()

  if (identifier !== 'com.nousresearch.hermes') {
    throw new Error('Bundle identifier mismatch')
  }
}

function sync(file: string) {
  const fd = fs.openSync(file, 'r')

  try {
    fs.fsyncSync(fd)
  } finally {
    fs.closeSync(fd)
  }
}

function syncTree(file: string) {
  const stat = fs.lstatSync(file)

  if (stat.isSymbolicLink()) {
    return
  }

  if (stat.isDirectory()) {
    for (const child of fs.readdirSync(file)) {
      syncTree(path.join(file, child))
    }
  }

  sync(file)
}

function canonical(file: string, exists = true) {
  if (
    !path.isAbsolute(file) ||
    path.normalize(file) !== file ||
    (exists ? fs.realpathSync(file) !== file : fs.realpathSync(path.dirname(file)) !== path.dirname(file))
  ) {
    throw new Error('Canonical absolute operator paths required')
  }
}

/** A hard link publishes exact bytes without overwrite or a partial-copy window.
 * Only the same inode can resume the link-before-unlink crash window.
 */
async function archiveRecoveredBundle(
  request: BundleRequest,
  expected: RuntimeSelection,
  target: RuntimeSelection,
  check: () => Promise<void>,
  phase?: (phase: string) => void
) {
  const binding = { ...request, action: 'install' }
  const digest = createHash('sha256').update(JSON.stringify(binding)).digest('hex')
  const journal = path.join(request.userData, 'bundle-install.json')
  const archive = path.join(request.userData, `bundle-install.${request.transaction}.${digest}.recovered.json`)

  const read = (file: string) => {
    let stat: fs.Stats

    try {
      stat = fs.lstatSync(file)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
        return undefined
      }

      throw error
    }

    if (!stat.isFile()) {
      throw new Error('Unsafe archive/journal path')
    }

    const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW)

    try {
      const opened = fs.fstatSync(fd)

      if (opened.dev !== stat.dev || opened.ino !== stat.ino) {
        throw new Error('Archive/journal changed')
      }

      return { stat: opened, bytes: fs.readFileSync(fd) }
    } finally {
      fs.closeSync(fd)
    }
  }

  const original = read(journal)
  const retained = read(archive)
  const source = original ?? retained

  if (!source) {
    throw new Error('Missing recovered bundle journal and archive')
  }

  const value = JSON.parse(source.bytes.toString('utf8'))

  if (
    value?.version !== 1 ||
    Object.keys(value).sort().join() !== 'before,binding,phase,target,version' ||
    value.phase !== 'recovered' ||
    JSON.stringify(value.binding) !== JSON.stringify(binding) ||
    JSON.stringify(value.before) !== JSON.stringify(expected) ||
    JSON.stringify(value.target) !== JSON.stringify(target)
  ) {
    throw new Error('Exact recovered bundle journal required')
  }

  const unchanged = (entry: ReturnType<typeof read>) =>
    entry &&
    entry.stat.dev === source.stat.dev &&
    entry.stat.ino === source.stat.ino &&
    entry.bytes.equals(source.bytes)

  const validateLayout = (requireArchive = false) => {
    const live = read(journal)
    const saved = read(archive)
    const links = live && saved ? 2 : 1

    if (
      (original ? !unchanged(live) : !!live) ||
      (saved && !unchanged(saved)) ||
      (requireArchive && !saved) ||
      (!live && !saved) ||
      [live, saved].some(entry => entry && entry.stat.nlink !== links)
    ) {
      throw new Error('Occupied archive destination or changed bundle journal')
    }
  }

  await check()
  validateLayout()

  if (original && !retained) {
    // link(2) is exclusive even against dangling symlinks; rename could overwrite.
    sync(journal)
    fs.linkSync(journal, archive)
    phase?.('archive-linked')
  }

  sync(archive)
  sync(request.userData)
  phase?.('archive-durable')
  await check()
  validateLayout(true)

  if (original) {
    fs.unlinkSync(journal)
    phase?.('archive-unlinked')
  }

  sync(request.userData)

  return { phase: 'archived-recovered', archive }
}

/** Ordered, recoverable publication; not atomic across bundle and registry. */
export async function runBundleOffline(
  request: BundleRequest,
  boundary: {
    inspect?: typeof inspectClosedDesktop
    verify?: typeof verifyBundle
    phase?: (phase: string) => void
  } = {}
) {
  const fields = [
    'action',
    'userData',
    'installedApp',
    'candidateApp',
    'expected',
    'candidateSha256',
    'oldSha256',
    'candidateCommit',
    'oldCommit',
    'transaction'
  ]

  if (
    !request ||
    Object.keys(request)
      .filter(key => key !== 'runtimeCandidate')
      .sort()
      .join() !== fields.sort().join() ||
    !['install', 'recover', 'archive-recovered'].includes(request.action) ||
    !/^[a-zA-Z0-9-]{1,80}$/.test(request.transaction) ||
    ![request.oldSha256, request.candidateSha256].every(v => /^[a-f0-9]{64}$/.test(v)) ||
    ![request.oldCommit, request.candidateCommit].every(v => /^[a-f0-9]{40}$/.test(v))
  ) {
    throw new Error('Invalid bundle request')
  }

  canonical(request.userData)
  canonical(request.candidateApp)
  canonical(request.installedApp, false)

  if (
    request.installedApp === request.candidateApp ||
    request.candidateApp.startsWith(`${request.installedApp}/`) ||
    request.userData.startsWith(`${request.installedApp}/`)
  ) {
    throw new Error('Overlapping paths')
  }

  const expected = normalizeRuntimeSelection(request.expected)
  const parent = path.dirname(request.installedApp)
  const prefix = path.join(parent, `.${path.basename(request.installedApp)}.${request.transaction}`)

  const stage = `${prefix}.stage`,
    backup = `${prefix}.backup`,
    displaced = `${prefix}.displaced`

  const journal = path.join(request.userData, 'bundle-install.json')
  const verify = boundary.verify ?? verifyBundle

  const identity = (file: string, digest: string, commit: string) => {
    canonical(file)

    if (!fs.lstatSync(file).isDirectory() || bundleHash(file) !== digest) {
      throw new Error('Bundle identity mismatch')
    }

    verify(file, commit)
  }

  const binding = { ...request, action: 'install' }

  return withOfflineMaintenance(
    request,
    async (root, absent) => {
      const current = () =>
        normalizeRuntimeSelection(
          readConnectionsRegistry(path.join(root, 'connections.json')).connections.find(c => c.id === 'local')
            ?.generalRuntime ?? { generation: 0, coordinate: null }
        )

      const same = (a: RuntimeSelection, b: RuntimeSelection) => JSON.stringify(a) === JSON.stringify(b)

      const target = request.runtimeCandidate
        ? normalizeRuntimeSelection({ generation: expected.generation + 1, coordinate: request.runtimeCandidate })
        : expected

      const restored = request.runtimeCandidate
        ? normalizeRuntimeSelection({ generation: expected.generation + 2, coordinate: expected.coordinate })
        : expected

      if (request.action === 'archive-recovered') {
        return archiveRecoveredBundle(
          request,
          expected,
          target,
          async () => {
            await absent()

            if (![expected, restored].some(selection => same(current(), selection))) {
              throw new Error('stale recovered runtime generation/coordinate')
            }

            for (const name of ['runtime-transition.json', 'runtime-rollback.json']) {
              try {
                fs.lstatSync(path.join(root, name))
                throw new Error('Unresolved runtime journal; archive blocked')
              } catch (error) {
                if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
                  throw error
                }
              }
            }

            identity(request.installedApp, request.oldSha256, request.oldCommit)
          },
          boundary.phase
        )
      }

      let allowed = expected

      const check = async () => {
        await absent()

        if (!same(current(), allowed)) {
          throw new Error('stale runtime generation/coordinate')
        }
      }

      if (request.action === 'install') {
        await check()

        for (const name of ['runtime-transition.json', 'runtime-rollback.json']) {
          if (fs.existsSync(path.join(root, name))) {
            throw new Error('Previous runtime transition exists')
          }
        }
      }

      let failure: unknown

      const record = (phase: string) => {
        if (phase !== 'prepared') {
          if (
            !fs.lstatSync(journal).isFile() ||
            JSON.stringify(JSON.parse(fs.readFileSync(journal, 'utf8')).binding) !== JSON.stringify(binding)
          ) {
            throw new Error('Foreign bundle journal; refusing to resolve another transaction')
          }
        }

        const temp = `${journal}.${randomUUID()}.tmp`
        const fd = fs.openSync(temp, 'wx', 0o600)

        try {
          fs.writeFileSync(fd, JSON.stringify({ version: 1, binding, phase, before: expected, target }))
          fs.fsyncSync(fd)
        } finally {
          fs.closeSync(fd)
        }

        fs.renameSync(temp, journal)
        sync(root)
        boundary.phase?.(phase)
      }

      const move = async (from: string, to: string) => {
        await check()

        if (
          fs.existsSync(to) ||
          (() => {
            try {
              fs.lstatSync(to)

              return true
            } catch (e) {
              if ((e as NodeJS.ErrnoException).code === 'ENOENT') {
                return false
              }

              throw e
            }
          })()
        ) {
          throw new Error('Occupied transaction destination')
        }

        fs.renameSync(from, to)
        sync(parent)
      }

      if (request.action === 'install') {
        for (const file of [stage, backup, displaced, journal]) {
          try {
            fs.lstatSync(file)
            throw new Error('Previous bundle transaction exists')
          } catch (e) {
            if ((e as NodeJS.ErrnoException).code !== 'ENOENT') {
              throw e
            }
          }
        }

        identity(request.candidateApp, request.candidateSha256, request.candidateCommit)
        identity(request.installedApp, request.oldSha256, request.oldCommit)
        record('prepared')
        copyBundle(request.candidateApp, stage)
        identity(stage, request.candidateSha256, request.candidateCommit)
        syncTree(stage)
        sync(parent)
        record('staged')
        identity(request.installedApp, request.oldSha256, request.oldCommit)
        await move(request.installedApp, backup)
        record('old-moved')
        await move(stage, request.installedApp)
        record('installed')
        identity(request.installedApp, request.candidateSha256, request.candidateCommit)
        await check()

        try {
          if (request.runtimeCandidate) {
            await runOfflineLocked(
              {
                action: 'activate',
                userData: root,
                installedApp: request.installedApp,
                expected,
                candidate: request.runtimeCandidate
              },
              pins,
              root,
              absent,
              JSON.stringify(binding)
            )
            allowed = target
          }

          record('runtime-published')
          await check()
          record('complete')

          return { phase: 'complete', backup, runtime: allowed, runtimeChanged: !!request.runtimeCandidate }
        } catch (error) {
          failure = error
        }
      }

      if (!fs.lstatSync(journal).isFile()) {
        throw new Error('Unsafe bundle journal')
      }

      const value = JSON.parse(fs.readFileSync(journal, 'utf8'))

      if (
        value.version !== 1 ||
        JSON.stringify(value.binding) !== JSON.stringify(binding) ||
        ![
          'prepared',
          'staged',
          'old-moved',
          'installed',
          'runtime-published',
          'complete',
          'restoring',
          'recovered'
        ].includes(value.phase) ||
        !same(value.before, expected) ||
        !same(value.target, target)
      ) {
        throw new Error('Unknown bundle journal')
      }

      // Reverse publication order. Only this transaction's exact versions may recover.
      if (![expected, target, restored].some(selection => same(current(), selection))) {
        throw new Error(
          'stale recovery generation/coordinate; retain bundle journal and backups; reconcile external runtime owner before retry'
        )
      }

      for (const name of ['runtime-transition.json', 'runtime-rollback.json']) {
        const runtimeJournal = new RuntimeTransitionJournal(path.join(root, name))
        const entry = runtimeJournal.read()

        if (entry && (!same(entry.before, expected) || !same(entry.after, target))) {
          throw new Error('Foreign runtime journal; recovery blocked without mutation')
        }
      }

      // Validate the complete recovery set before touching runtime state. A rename
      // may have completed before its phase record became durable.
      const present = (file: string) => {
        try {
          fs.lstatSync(file)

          return true
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
            return false
          }

          throw error
        }
      }

      for (const [file, digest, commit] of [
        [backup, request.oldSha256, request.oldCommit],
        [displaced, request.candidateSha256, request.candidateCommit],
        [stage, request.candidateSha256, request.candidateCommit]
      ]) {
        if (present(file)) {
          identity(file, digest, commit)
        }
      }

      const restore = `${prefix}.restore`

      if (present(restore)) {
        try {
          identity(restore, request.oldSha256, request.oldCommit)
        } catch (error) {
          throw new Error(
            `partial-restore-copy; ${JSON.stringify(copyFailure(error))}; retain ${backup}; move ${restore} to evidence before retry`
          )
        }
      }

      if (present(request.installedApp)) {
        const isOld = bundleHash(request.installedApp) === request.oldSha256
        identity(
          request.installedApp,
          isOld ? request.oldSha256 : request.candidateSha256,
          isOld ? request.oldCommit : request.candidateCommit
        )

        if (!isOld && (!present(backup) || present(displaced))) {
          throw new Error('Invalid bundle recovery layout')
        }
      } else if (!present(backup)) {
        throw new Error('Missing bundle recovery source')
      }

      // Completed installs must become pending again BEFORE the first rollback
      // write; ordinary errors release the lease but never this durable fence.
      record('restoring')

      if (request.runtimeCandidate) {
        const hasPending = fs.existsSync(path.join(root, 'runtime-transition.json'))
        const hasRollback = fs.existsSync(path.join(root, 'runtime-rollback.json'))

        if (same(current(), target) && !hasPending && !hasRollback) {
          throw new Error('Missing owned runtime recovery record; retained bundle journal requires runtime repair')
        }

        if (hasPending || hasRollback) {
          await runOfflineLocked(
            { action: 'rollback', userData: root, installedApp: request.installedApp, expected: current() },
            pins,
            root,
            absent,
            JSON.stringify(binding)
          )
        }
      }

      allowed = current()
      await check()

      // Reconcile identities, not just last recorded phase: rename can precede journal fsync.
      if (fs.existsSync(backup)) {
        identity(backup, request.oldSha256, request.oldCommit)

        if (fs.existsSync(request.installedApp) && bundleHash(request.installedApp) === request.oldSha256) {
          identity(request.installedApp, request.oldSha256, request.oldCommit)
        } else {
          if (fs.existsSync(request.installedApp)) {
            identity(request.installedApp, request.candidateSha256, request.candidateCommit)
            record('restoring')
            await move(request.installedApp, displaced)
          }

          const restore = `${prefix}.restore`

          // Never consume/delete the retained backup. A partial restore copy is
          // unknown and fails closed, rather than overwriting unverified bytes.
          try {
            if (!fs.existsSync(restore)) {
              copyBundle(backup, restore)
              syncTree(restore)
              sync(parent)
            }

            identity(restore, request.oldSha256, request.oldCommit)
          } catch (error) {
            throw new Error(
              JSON.stringify({
                status: 'blocked',
                cause: copyFailure(error),
                reason: 'partial-restore-copy',
                retainedBackup: backup,
                partialCopy: restore,
                action: `Move ${restore} to a separate retained evidence path, then rerun the identical recover request; do not alter ${backup}`
              })
            )
          }

          await move(restore, request.installedApp)
        }
      }

      identity(request.installedApp, request.oldSha256, request.oldCommit)
      await check()
      record('recovered')

      if (failure) {
        throw new Error(`Runtime publication failed; old bundle restored, backups retained: ${String(failure)}`)
      }

      return { phase: 'recovered', runtime: allowed, runtimeChanged: !!request.runtimeCandidate }
    },
    boundary.inspect
  )
}
