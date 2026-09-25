import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, expect, test } from 'vitest'

import { normalizeRegistry, parseStoredRegistry } from './connection-registry'
import type { ProcessOwner, ProcessReceipt } from './process-owner'
import { inspectClosedDesktop, runOffline } from './runtime-offline'
import { RuntimeTransitionController, RuntimeTransitionJournal, type TransitionOwner } from './runtime-transition'

const cleanup: (() => void)[] = []
afterEach(() => {
  for (const fn of cleanup.splice(0)) {
    fn()
  }
})

function fixture() {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'native-transition-')))
  const release = path.join(dir, 'candidate')
  fs.mkdirSync(path.join(release, 'source'), { recursive: true })
  fs.mkdirSync(path.join(release, 'venv/bin'), { recursive: true })
  const python = path.join(release, 'venv/bin/python')
  fs.writeFileSync(python, 'never executed', { mode: 0o500 })
  const hash = (text: string) => createHash('sha256').update(text).digest('hex')

  const manifest = JSON.stringify({
    schema: 2,
    status: 'ready',
    target: release,
    commit: 'a'.repeat(40),
    python_requested: python,
    python_resolved: python,
    python_sha256: hash('never executed'),
    source_files: {},
    generated_source_files: {},
    venv_files: { bin: { directory: true }, 'bin/python': { executable: true, sha256: hash('never executed') } }
  })

  fs.writeFileSync(path.join(release, 'manifest.json'), manifest, { mode: 0o400 })

  for (const name of ['source', 'venv/bin', 'venv', '']) {
    fs.chmodSync(path.join(release, name), 0o500)
  }

  cleanup.push(() => {
    for (const name of ['', 'source', 'venv', 'venv/bin']) {
      fs.chmodSync(path.join(release, name), 0o700)
    }

    fs.rmSync(dir, { recursive: true, force: true })
  })

  const candidate = {
    agentRoot: path.join(release, 'source'),
    python,
    commit: 'a'.repeat(40),
    manifestSha256: hash(manifest)
  }

  const file = path.join(dir, 'connections.json')
  const initial = normalizeRegistry(null)
  initial.connections.push({
    id: 'remote',
    kind: 'remote',
    label: 'Unchanged',
    url: 'https://example.invalid',
    authMode: 'token',
    token: 'opaque'
  })
  fs.writeFileSync(file, JSON.stringify(initial))
  let writes = 0

  const store = {
    read: () => parseStoredRegistry(fs.readFileSync(file, 'utf8')),
    write: (value: typeof initial) => {
      writes++
      fs.writeFileSync(file, JSON.stringify(value))
    }
  }

  const journalFile = path.join(dir, 'runtime-transition.json')
  const journal = new RuntimeTransitionJournal(journalFile)
  const before = { generation: 0, coordinate: null }
  const operations: string[] = []
  let exited = false

  const idle: ProcessReceipt = {
    scope: 'process',
    status: 'idle',
    admission: 'closed',
    generation: 'b'.repeat(64),
    nonce: 'c'.repeat(64),
    inventory_complete: true,
    inventory: {
      turns: 0,
      children: 0,
      delegations: 0,
      processes: 0,
      notifications: 0,
      os_children: 0,
      durable_notifications: 0,
      rpc: 0
    }
  }

  let receipt = idle

  let onQuiesce = () => {}

  // Controlled OS boundary: no child launch or signal API exists in this fixture.
  // The real channel's generation/nonce/auth rejection is covered by process-owner-boundary.test.ts.
  const owner: TransitionOwner = {
    identity: {},
    exited: () => exited,
    channel: {
      request: async (operation: string) => {
        operations.push(operation)

        if (operation === 'quiesce') {
          onQuiesce()

          return receipt
        }

        return { admission: 'open' }
      },
      close() {}
    } as ProcessOwner
  }

  let active: TransitionOwner | null = owner
  let state: 'absent' | 'alive' | 'unknown' = 'alive'

  const deps = {
    store,
    journal,
    trusted: () => new Set([candidate.manifestSha256]),
    owner: () => active,
    backendState: () => state
  }

  return {
    candidate,
    before,
    idle,
    operations,
    initial,
    store,
    file,
    journal,
    journalFile,
    deps,
    controller: new RuntimeTransitionController(deps),
    writes: () => writes,
    receipt: (value: ProcessReceipt) => {
      receipt = value
    },
    exit: () => {
      exited = true
      state = 'absent'
    },
    state: (value: typeof state) => {
      state = value
    },
    replaceOwner: () => {
      active = { ...owner, identity: {} }
    },
    onQuiesce: (fn: () => void) => {
      onQuiesce = fn
    }
  }
}

test('host attachment never bypasses exact runtime selection or a recovery fence', async () => {
  const f = fixture()
  let probes = 0

  const attach = async () => {
    probes++

    return { pid: 42 }
  }

  await expect(f.controller.attachHostBackend(attach)).resolves.toEqual({ pid: 42 })
  const selected = f.store.read()
  selected.connections[0].generalRuntime = { generation: 1, coordinate: f.candidate }
  f.store.write(selected)
  await expect(f.controller.attachHostBackend(attach)).resolves.toBeNull()
  expect(probes).toBe(1)
  f.journal.write(f.before, selected.connections[0].generalRuntime)
  await expect(f.controller.attachHostBackend(attach)).rejects.toThrow(/recovery/)
  expect(probes).toBe(1)
})

test('an in-flight host attachment rejects a newer runtime selection', async () => {
  const f = fixture()
  await expect(
    f.controller.attachHostBackend(async () => {
      const changed = f.store.read()
      changed.connections[0].generalRuntime = { generation: 1, coordinate: f.candidate }
      f.store.write(changed)

      return { pid: 42 }
    })
  ).rejects.toThrow(/stale/)
})

test('offline entry activates with exact readback, refuses unsafe states and recovers persistent CAS', async () => {
  const f = fixture()
  const userData = path.dirname(f.file)
  fs.writeFileSync(path.join(userData, 'backend-ownership.json'), '{"backends":[]}')

  const request = {
    action: 'activate' as const,
    userData,
    installedApp: '/Applications/Hermes.app',
    expected: f.before,
    candidate: f.candidate
  }

  const pins = [f.candidate]
  const bytes = fs.readFileSync(f.file, 'utf8')

  for (const state of ['alive', 'unknown'] as const) {
    await expect(runOffline(request, pins, async () => state)).rejects.toThrow(/alive or unknown/)
    expect(fs.readFileSync(f.file, 'utf8')).toBe(bytes)
  }

  const absent = (app: string, ownership: string) => inspectClosedDesktop(app, ownership, async () => [])
  const after = await runOffline(request, pins, absent)
  expect(f.store.read().connections[0].generalRuntime).toEqual(after)
  expect(f.store.read().connections[1]).toEqual(f.initial.connections[1])
  expect(f.journal.read()).toBeNull()
  await expect(runOffline({ ...request, action: 'rollback' }, pins, absent)).rejects.toThrow(/stale/)
  const restored = await runOffline({ ...request, action: 'rollback', expected: after! }, pins, absent)
  expect(restored).toEqual({ generation: 2, coordinate: null })
  expect(f.store.read().connections[0].generalRuntime).toEqual(restored)
  expect(f.store.read().connections[1]).toEqual(f.initial.connections[1])
  // Simulate interruption after journal fsync and CAS, before finalization.
  const next = f.controller.transitionClosed.bind(f.controller)
  f.state('absent')
  const crashed = next(restored!, f.candidate)
  expect(await runOffline({ ...request, action: 'recover', expected: crashed }, pins, absent)).toEqual({
    generation: 4,
    coordinate: null
  })
  expect(f.store.read().connections[1]).toEqual(f.initial.connections[1])
})

test('invalid, busy, unknown and live-idle proofs never write a registry/journal or signal', async () => {
  const f = fixture()
  const original = fs.readFileSync(f.file, 'utf8')

  for (const receipt of [
    { ...f.idle, scope: 'session' },
    { ...f.idle, generation: '' },
    { ...f.idle, inventory: { rpc: 0 } },
    { ...f.idle, inventory_complete: false },
    { ...f.idle, status: 'unknown' },
    { ...f.idle, status: 'busy', inventory: { ...f.idle.inventory, turns: 1 } },
    f.idle
  ]) {
    f.receipt(receipt as ProcessReceipt)
    await expect(f.controller.transition(f.before, f.candidate)).rejects.toThrow(/proof|alive/)
    expect(f.journal.read()).toBeNull()
    expect(fs.readFileSync(f.file, 'utf8')).toBe(original)
    expect(f.writes()).toBe(0)
    expect(f.operations.slice(-2)).toEqual(['quiesce', 'cancel'])
  }

  f.state('unknown')
  await expect(f.controller.transition(f.before, f.candidate)).rejects.toThrow(/unknown/)
  expect(f.operations.every(op => ['quiesce', 'cancel'].includes(op))).toBe(true)
  const ops = f.operations.length
  await expect(f.controller.transition(f.before, { ...f.candidate, manifestSha256: 'f'.repeat(64) })).rejects.toThrow(
    /trusted/
  )
  expect(f.operations).toHaveLength(ops)
})

test('persistent crash recovery restores unset, is idempotent, fences starts and refuses stale CAS', async () => {
  const f = fixture()
  const ticket = f.controller.startTicket()
  f.onQuiesce(f.exit)
  const after = await f.controller.transition(f.before, f.candidate)
  expect(() => f.controller.assertStart(ticket)).toThrow(/stale/)
  expect(() => f.controller.startTicket()).toThrow(/recovery/)
  expect(f.store.read().connections[1]).toEqual(f.initial.connections[1])
  const persisted = JSON.parse(fs.readFileSync(f.journalFile, 'utf8'))
  expect(Object.keys(persisted).sort()).toEqual(['after', 'before', 'connection', 'profile', 'version'])
  expect(fs.statSync(f.journalFile).mode & 0o777).toBe(0o600)
  const reboot = new RuntimeTransitionController(f.deps)
  expect(reboot.recover()).toEqual({ generation: after.generation + 1, coordinate: null })
  expect(reboot.recover()).toBeNull()
  expect(f.store.read().connections[1]).toEqual(f.initial.connections[1])
  // Crash after rollback write, before journal unlink: must finish idempotently.
  f.journal.write(f.before, after)
  expect(reboot.recover()).toEqual({ generation: after.generation + 1, coordinate: null })
  f.journal.write(f.before, after)
  const newer = f.store.read()
  newer.connections[0].generalRuntime = { generation: after.generation + 2, coordinate: null }
  f.store.write(newer)
  const bytes = fs.readFileSync(f.file, 'utf8')
  expect(() => reboot.recover()).toThrow(/stale/)
  expect(fs.readFileSync(f.file, 'utf8')).toBe(bytes)
  expect(f.journal.read()).not.toBeNull()
})

test('crash before registry publication clears only the journal; recovery refuses a live backend', async () => {
  const f = fixture()
  f.onQuiesce(f.exit)

  const failing = new RuntimeTransitionController({
    ...f.deps,
    store: {
      read: f.store.read,
      write: () => {
        throw new Error('controlled disk failure')
      }
    }
  })

  await expect(failing.transition(f.before, f.candidate)).rejects.toThrow(/disk failure/)
  expect(f.writes()).toBe(0)
  expect(f.journal.read()).not.toBeNull()
  f.state('alive')
  expect(() => f.controller.recover()).toThrow(/absent/)
  expect(f.journal.read()).not.toBeNull()
  f.state('absent')
  expect(f.controller.recover()).toEqual(f.before)
  expect(f.writes()).toBe(0)
})

test('rollback preserves a prior sealed coordinate as well as the unset case', async () => {
  const f = fixture()
  const selected = f.store.read()
  const prior = { generation: 3, coordinate: f.candidate }
  selected.connections[0].generalRuntime = prior
  f.store.write(selected)
  f.onQuiesce(f.exit)
  expect(await f.controller.transition(prior, null)).toEqual({ generation: 4, coordinate: null })
  expect(new RuntimeTransitionController(f.deps).recover()).toEqual({ generation: 5, coordinate: f.candidate })
  expect(f.store.read().connections[1]).toEqual(f.initial.connections[1])
})

test('mutable request objects cannot retarget an in-flight CAS', async () => {
  const f = fixture()
  f.onQuiesce(() => {
    f.exit()
    f.before.generation = 7
    const changed = f.store.read()
    changed.connections[0].generalRuntime = { ...f.before }
    f.store.write(changed)
  })
  await expect(f.controller.transition(f.before, f.candidate)).rejects.toThrow(/stale/)
  expect(f.journal.read()).toBeNull()
  expect(f.writes()).toBe(1)
})

test('owner replacement and a registry generation racing quiesce cannot commit', async () => {
  const f = fixture()
  f.onQuiesce(f.replaceOwner)
  await expect(f.controller.transition(f.before, f.candidate)).rejects.toThrow(/stale process/)
  expect(f.writes()).toBe(0)
  expect(f.journal.read()).toBeNull()
  f.onQuiesce(() => {
    const changed = f.store.read()
    changed.connections[0].generalRuntime = { generation: 1, coordinate: null }
    f.store.write(changed)
  })
  await expect(f.controller.transition(f.before, f.candidate)).rejects.toThrow(/stale runtime/)
  expect(f.writes()).toBe(1)
  expect(f.journal.read()).toBeNull()
})
