import { randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import type { ConnectionRegistry } from './connection-registry'
import {
  changeRuntimeSelection,
  normalizeRuntimeSelection,
  type RuntimeCoordinate,
  type RuntimeSelection,
  validateRuntimeCoordinate
} from './connection-runtime'
import type { ProcessOwner, ProcessReceipt } from './process-owner'

export interface TransitionOwner {
  /** Main-owned child object, never a PID supplied by a caller. */
  identity: object
  channel: ProcessOwner
  exited(): boolean
}
export interface TransitionStore {
  read(): ConnectionRegistry
  write(registry: ConnectionRegistry): void
}
interface RecoveryRecord {
  version: 1
  connection: 'local'
  profile: 'general'
  before: RuntimeSelection
  after: RuntimeSelection
}

const same = (a: RuntimeSelection, b: RuntimeSelection) => JSON.stringify(a) === JSON.stringify(b)

const inventoryKeys = [
  'turns',
  'children',
  'delegations',
  'processes',
  'notifications',
  'os_children',
  'durable_notifications',
  'rpc'
]

function assertIdle(receipt: ProcessReceipt): void {
  // The authenticated channel additionally checks its handshake generation and
  // stable lease nonce. Never accept a renderer/public-RPC receipt here.
  if (
    !receipt ||
    receipt.scope !== 'process' ||
    receipt.status !== 'idle' ||
    receipt.admission !== 'closed' ||
    receipt.inventory_complete !== true ||
    !/^[a-f0-9]{64}$/.test(receipt.generation) ||
    !/^[a-f0-9]{64}$/.test(receipt.nonce) ||
    !receipt.inventory ||
    Object.keys(receipt.inventory).length !== inventoryKeys.length ||
    !inventoryKeys.every(key => receipt.inventory[key] === 0) ||
    receipt.reason !== undefined
  ) {
    throw new Error('Complete authenticated process idle proof required')
  }
}

/** Metadata only: no owner capability, nonce, receipt, PID, token or registry copy. */
export class RuntimeTransitionJournal {
  constructor(private readonly file: string) {}
  read(): RecoveryRecord | null {
    let text: string

    try {
      text = fs.readFileSync(this.file, 'utf8')
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
        return null
      }

      throw error
    }

    const value = JSON.parse(text)

    if (
      value?.version !== 1 ||
      value.connection !== 'local' ||
      value.profile !== 'general' ||
      Object.keys(value).sort().join(',') !== 'after,before,connection,profile,version'
    ) {
      throw new Error('Invalid runtime recovery journal')
    }

    const before = normalizeRuntimeSelection(value.before)
    const after = normalizeRuntimeSelection(value.after)

    if (after.generation !== before.generation + 1) {
      throw new Error('Invalid recovery generation')
    }

    return { version: 1, connection: 'local', profile: 'general', before, after }
  }
  write(before: RuntimeSelection, after: RuntimeSelection): void {
    fs.mkdirSync(path.dirname(this.file), { recursive: true, mode: 0o700 })
    const temporary = `${this.file}.${randomUUID()}.tmp`
    const fd = fs.openSync(temporary, 'wx', 0o600)

    try {
      fs.writeFileSync(
        fd,
        JSON.stringify({
          version: 1,
          connection: 'local',
          profile: 'general',
          before: normalizeRuntimeSelection(before),
          after: normalizeRuntimeSelection(after)
        })
      )
      fs.fsyncSync(fd)
    } finally {
      fs.closeSync(fd)
    }

    try {
      fs.renameSync(temporary, this.file)
      this.syncDirectory()
    } finally {
      if (fs.existsSync(temporary)) {
        fs.unlinkSync(temporary)
      }
    }
  }
  clear(): void {
    fs.unlinkSync(this.file)
    this.syncDirectory()
  }
  private syncDirectory(): void {
    const fd = fs.openSync(path.dirname(this.file), 'r')

    try {
      fs.fsyncSync(fd)
    } finally {
      fs.closeSync(fd)
    }
  }
}

/** One Electron main instance, sharing the existing registry writer. No signal API. */
export class RuntimeTransitionController {
  private epoch = 0
  private changing = false
  constructor(
    private readonly deps: {
      store: TransitionStore
      journal: RuntimeTransitionJournal
      trusted: () => ReadonlySet<string>
      owner: () => TransitionOwner | null
      /** Must account for pending starts and unowned/legacy children; unknown is unsafe. */
      backendState: () => 'absent' | 'alive' | 'unknown'
    }
  ) {}

  private selection(): RuntimeSelection {
    const local = this.deps.store.read().connections.find(c => c.id === 'local' && c.kind === 'local')

    if (!local) {
      throw new Error('Missing local runtime scope')
    }

    return normalizeRuntimeSelection(local.generalRuntime ?? { generation: 0, coordinate: null })
  }
  private assertExpected(expected: RuntimeSelection): void {
    if (!same(this.selection(), normalizeRuntimeSelection(expected))) {
      throw new Error('stale runtime generation/coordinate')
    }
  }
  private validate = (coordinate: RuntimeCoordinate) => validateRuntimeCoordinate(coordinate, this.deps.trusted())

  /** Capture before async resolve; check immediately before spawn with no intervening await. */
  startTicket(): { epoch: number; selection: RuntimeSelection } {
    if (this.changing || this.deps.journal.read()) {
      throw new Error('Runtime transition/recovery blocks start')
    }

    return { epoch: this.epoch, selection: this.selection() }
  }
  assertStart(ticket: ReturnType<RuntimeTransitionController['startTicket']>): void {
    if (ticket.epoch !== this.epoch || this.changing || this.deps.journal.read()) {
      throw new Error('stale runtime start')
    }

    this.assertExpected(ticket.selection)
  }

  /** Host discovery has no exact-runtime identity proof; selected releases must spawn. */
  async attachHostBackend<T>(attach: () => Promise<T | null>): Promise<T | null> {
    const ticket = this.startTicket()

    if (ticket.selection.coordinate) {
      return null
    }

    const attached = await attach()
    this.assertStart(ticket)

    return attached
  }

  async transition(expected: RuntimeSelection, candidate: RuntimeCoordinate | null): Promise<RuntimeSelection> {
    expected = normalizeRuntimeSelection(expected)

    if (this.changing || this.deps.journal.read()) {
      throw new Error('Runtime transition/recovery already pending')
    }

    this.assertExpected(expected)
    const coordinate = candidate === null ? null : this.validate(candidate)
    const owner = this.deps.owner()

    if (!owner) {
      throw new Error('Exact native process owner required; manual bootstrap pending')
    }

    this.changing = true
    this.epoch++
    let quiesced = false

    try {
      const receipt = await owner.channel.request('quiesce')
      quiesced = true
      assertIdle(receipt)
      this.assertExpected(expected)

      if (this.deps.owner() !== owner) {
        throw new Error('stale process owner')
      }

      // Idle is NOT authorization to kill. No native graceful-exit operation is
      // available yet. An observed exit is necessary even with a valid proof.
      if (!owner.exited() || this.deps.backendState() !== 'absent') {
        throw new Error('Old backend alive/unknown; native graceful exit unavailable')
      }

      const after = normalizeRuntimeSelection({ generation: expected.generation + 1, coordinate })
      this.deps.journal.write(expected, after)
      const result = changeRuntimeSelection(this.deps.store, 'local', 'general', expected, coordinate, this.validate)

      // Persist until native health acceptance exists. Recovery rolls back; it
      // never silently resumes or starts a candidate after a crash.
      return result
    } finally {
      try {
        if (quiesced && !owner.exited()) {
          await owner.channel.request('cancel')
        }
      } finally {
        this.changing = false
      }
    }
  }

  /** Offline caller must independently prove the app and every owned backend absent. */
  transitionClosed(expected: RuntimeSelection, candidate: RuntimeCoordinate): RuntimeSelection {
    expected = normalizeRuntimeSelection(expected)

    if (this.changing || this.deps.journal.read()) {
      throw new Error('Runtime transition/recovery already pending')
    }

    this.assertExpected(expected)
    const coordinate = this.validate(candidate)

    if (this.deps.backendState() !== 'absent') {
      throw new Error('Closed app/backend absence required')
    }

    this.epoch++
    const after = normalizeRuntimeSelection({ generation: expected.generation + 1, coordinate })
    this.deps.journal.write(expected, after)

    return changeRuntimeSelection(this.deps.store, 'local', 'general', expected, coordinate, this.validate)
  }

  /** Explicit recovery only, before any local/general spawn. CAS never overwrites newer intent. */
  recover(): RuntimeSelection | null {
    if (this.changing) {
      throw new Error('Runtime transition in progress')
    }

    const record = this.deps.journal.read()

    if (!record) {
      return null
    }

    if (this.deps.backendState() !== 'absent') {
      throw new Error('Recovery requires proven absent backend')
    }

    this.epoch++
    const current = this.selection()

    const rolledBack = normalizeRuntimeSelection({
      generation: record.after.generation + 1,
      coordinate: record.before.coordinate
    })

    if (same(current, record.before) || same(current, rolledBack)) {
      this.deps.journal.clear()

      return current
    }

    if (!same(current, record.after)) {
      throw new Error('stale recovery generation/coordinate')
    }

    const restored = changeRuntimeSelection(
      this.deps.store,
      'local',
      'general',
      record.after,
      record.before.coordinate,
      this.validate
    )

    this.deps.journal.clear()

    return restored
  }
}
