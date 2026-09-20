import fs from 'node:fs'
import path from 'node:path'

import { execText, isPidOnlyStartMarker, processStartMarker } from './backend-claim'
import { backendCommandMatches, parseBackendOwnershipDetailed } from './backend-ownership'
import { readConnectionsRegistry, writeConnectionsRegistry } from './connection-registry-store'
import {
  normalizeRuntimeSelection,
  type RuntimeCoordinate,
  type RuntimeSelection,
  validateRuntimeCoordinate
} from './connection-runtime'
import { isIndependentGeneralService } from './runtime-independent-service'
import { RuntimeTransitionController, RuntimeTransitionJournal } from './runtime-transition'

type State = 'absent' | 'alive' | 'unknown'
interface ProcessRow {
  pid: number
  command: string
}

async function processTable(): Promise<ProcessRow[]> {
  if (process.platform !== 'darwin') {
    throw new Error('Offline maintenance currently supports macOS only')
  }

  const text = await execText('/bin/ps', ['-axo', 'pid=,command='], { timeout: 5000 })

  if (!text) {
    throw new Error('Empty process inventory')
  }

  return text.split('\n').map(line => {
    const m = /^\s*(\d+)\s+(.+)$/.exec(line)

    if (!m) {
      throw new Error('Incomplete process inventory')
    }

    return { pid: Number(m[1]), command: m[2] }
  })
}

/** No signals (except the existing marker probe's signal-0 liveness check). */
export async function inspectClosedDesktop(
  app: string,
  ownership: string,
  scan: () => Promise<ProcessRow[]> = processTable
): Promise<State> {
  try {
    const raw = JSON.parse(ownership)
    const values = Array.isArray(raw) ? raw : raw?.backends
    const parsed = parseBackendOwnershipDetailed(ownership)

    if (!Array.isArray(values) || parsed.corrupt || parsed.entries.length !== values.length) {
      return 'unknown'
    }

    const processes = await scan()

    if (processes.some(p => p.command === app || p.command.startsWith(`${app}/`) || p.command.startsWith(`${app} `))) {
      return 'alive'
    }

    // Ownership includes old parents, not just the currently installed app path.
    for (const entry of parsed.entries) {
      const identities = [{ pid: entry.pid, startMarker: entry.startMarker }]

      if (entry.parentPid && entry.parentStartMarker) {
        identities.push({ pid: entry.parentPid, startMarker: entry.parentStartMarker })
      }

      for (const identity of identities) {
        try {
          const marker = await processStartMarker(identity.pid, 5000)

          if (isPidOnlyStartMarker(identity.startMarker) || marker === identity.startMarker) {
            return 'alive'
          }
        } catch (error) {
          if (!['ESRCH', 'ENOENT'].includes((error as NodeJS.ErrnoException).code ?? '')) {
            return 'unknown'
          }
        }
      }
    }

    // Desktop evidence above wins even when launchd claims the same PID.
    // Unknown/unowned serve remains blocking unless the exact independent
    // General service supplies fresh, stable job + incarnation evidence.
    for (const row of processes.filter(p => backendCommandMatches(p.command))) {
      if (parsed.entries.some(entry => entry.pid === row.pid || entry.parentPid === row.pid)) {return 'alive'}

      if (!(await isIndependentGeneralService(row))) {return 'alive'}
    }

    return 'absent'
  } catch {
    return 'unknown'
  }
}

export interface OfflineRequest {
  action: 'activate' | 'rollback' | 'recover'
  userData: string
  installedApp: string
  expected: RuntimeSelection
  candidate?: RuntimeCoordinate
}

export async function runOffline(
  request: OfflineRequest,
  pins: { commit: string; manifestSha256: string }[],
  inspect = inspectClosedDesktop
): Promise<RuntimeSelection | null> {
  const keys = ['action', 'userData', 'installedApp', 'expected', 'candidate']

  if (
    !request ||
    !['activate', 'rollback', 'recover'].includes(request.action) ||
    Object.keys(request).some(key => !keys.includes(key))
  ) {
    throw new Error('Invalid offline request fields')
  }

  if (!path.isAbsolute(request.userData) || !path.isAbsolute(request.installedApp)) {
    throw new Error('Absolute paths required')
  }

  const expected = normalizeRuntimeSelection(request.expected)
  const root = fs.realpathSync(request.userData)
  const file = path.join(root, 'connections.json')
  const lock = path.join(root, 'runtime-maintenance.lock')

  const assertAbsent = async () => {
    // Fixed registry-relative metadata only; never follow redirected journals or
    // ownership/registry files outside the explicitly selected userData root.
    for (const name of [
      'connections.json',
      'backend-ownership.json',
      'runtime-transition.json',
      'runtime-rollback.json'
    ]) {
      try {
        if (!fs.lstatSync(path.join(root, name)).isFile()) {
          throw new Error('Unsafe maintenance metadata path')
        }
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
          throw error
        }
      }
    }

    const ownership = fs.readFileSync(path.join(root, 'backend-ownership.json'), 'utf8')

    if ((await inspect(request.installedApp, ownership)) !== 'absent') {
      throw new Error('App/backend alive or unknown; quit manually, no signals sent')
    }
  }

  await assertAbsent()
  // Atomic symlink lease: the complete identity exists at acquisition, so a
  // crash cannot leave an empty/partially-written owner record.
  const lease = JSON.stringify({ pid: process.pid, marker: await processStartMarker(process.pid, 5000) })

  try {
    fs.symlinkSync(lease, lock)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'EEXIST') {
      throw error
    }

    const old = fs.readlinkSync(lock)
    const owner = JSON.parse(old)

    if (!Number.isInteger(owner.pid) || owner.pid <= 0 || typeof owner.marker !== 'string') {
      throw new Error('Unknown maintenance owner')
    }

    let dead = false

    try {
      dead = (await processStartMarker(owner.pid, 5000)) !== owner.marker
    } catch (error) {
      dead = ['ESRCH', 'ENOENT'].includes((error as NodeJS.ErrnoException).code ?? '')
    }

    if (!dead) {
      throw new Error('Maintenance owner alive or unknown')
    }

    // Serialize reclaimers too; otherwise two dead-owner observations could
    // unlink a newly acquired lease. An interrupted reclaim fails closed.
    const reclaim = `${lock}.recovery`
    fs.mkdirSync(reclaim, { mode: 0o700 })

    try {
      if (fs.readlinkSync(lock) !== old) {
        throw new Error('Maintenance ownership changed')
      }

      fs.unlinkSync(lock)
      fs.symlinkSync(lease, lock)
    } finally {
      fs.rmdirSync(reclaim)
    }
  }

  try {
    await assertAbsent()

    const store = {
      read: () => readConnectionsRegistry(file),
      write: (r: ReturnType<typeof readConnectionsRegistry>) => writeConnectionsRegistry(file, r)
    }

    const current = normalizeRuntimeSelection(
      store.read().connections.find(c => c.id === 'local')?.generalRuntime ?? { generation: 0, coordinate: null }
    )

    if (JSON.stringify(current) !== JSON.stringify(expected)) {
      throw new Error('stale runtime generation/coordinate')
    }

    const trusted = new Set(pins.map(p => p.manifestSha256))
    const pending = new RuntimeTransitionJournal(path.join(root, 'runtime-transition.json'))
    const rollback = new RuntimeTransitionJournal(path.join(root, 'runtime-rollback.json'))

    if (request.action === 'activate') {
      if (
        !request.candidate ||
        !pins.some(
          p => p.commit === request.candidate!.commit && p.manifestSha256 === request.candidate!.manifestSha256
        )
      ) {
        throw new Error('Candidate pin mismatch')
      }

      validateRuntimeCoordinate(request.candidate, trusted)

      if (pending.read() || rollback.read()) {
        throw new Error('Previous transition/rollback record exists')
      }
    }

    await assertAbsent()
    const journal = request.action === 'rollback' && !pending.read() ? rollback : pending

    const controller = new RuntimeTransitionController({
      store,
      journal,
      trusted: () => trusted,
      owner: () => null,
      backendState: () => 'absent'
    })

    const result =
      request.action === 'activate' ? controller.transitionClosed(expected, request.candidate!) : controller.recover()

    const readback = normalizeRuntimeSelection(
      store.read().connections.find(c => c.id === 'local')?.generalRuntime ?? { generation: 0, coordinate: null }
    )

    if (result && JSON.stringify(readback) !== JSON.stringify(result)) {
      throw new Error('Registry readback mismatch')
    }

    if (request.action === 'activate') {
      rollback.write(expected, result!)
      pending.clear()
    } else if (request.action === 'rollback' && rollback.read()) {
      // A crash after rollback publication is completed by the same controller.
      new RuntimeTransitionController({
        store,
        journal: rollback,
        trusted: () => trusted,
        owner: () => null,
        backendState: () => 'absent'
      }).recover()
    }

    return result
  } finally {
    if (fs.readlinkSync(lock) === lease) {
      fs.unlinkSync(lock)
    }
  }
}
