import type { ChildProcess } from 'node:child_process'
import { createHmac, randomBytes } from 'node:crypto'
import type { Duplex } from 'node:stream'

export interface ProcessReceipt {
  scope: 'process'
  status: 'idle' | 'busy' | 'unknown'
  admission: 'closed'
  generation: string
  nonce: string
  inventory: Record<string, number>
  inventory_complete: boolean
  reason?: string
}

export interface OwnerResponses {
  status: { authority: 'launch-channel'; process_fence: true }
  quiesce: ProcessReceipt
  cancel: { admission: 'open' }
}

export interface ProcessOwner {
  request<O extends keyof OwnerResponses>(operation: O): Promise<OwnerResponses[O]>
  close(): void
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function exactKeys(value: Record<string, unknown>, keys: string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key))
}

function isProof(value: unknown): value is string {
  return typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
}

// Synchronized with process_admission.quiesce, not the public session receipt.
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

function validResponse<O extends keyof OwnerResponses>(
  operation: O,
  value: unknown,
  generation: string,
  nonce: string | undefined
): value is OwnerResponses[O] {
  if (!isRecord(value)) {
    return false
  }

  if (operation === 'status') {
    return (
      exactKeys(value, ['authority', 'process_fence']) &&
      value.authority === 'launch-channel' &&
      value.process_fence === true
    )
  }

  if (operation === 'cancel') {
    return exactKeys(value, ['admission']) && value.admission === 'open'
  }

  if (
    !exactKeys(value, [
      'scope',
      'status',
      'admission',
      'generation',
      'nonce',
      'inventory',
      'inventory_complete',
      ...('reason' in value ? ['reason'] : [])
    ]) ||
    value.scope !== 'process' ||
    value.admission !== 'closed' ||
    value.generation !== generation ||
    !isProof(value.nonce) ||
    (nonce !== undefined && value.nonce !== nonce) ||
    !isRecord(value.inventory)
  ) {
    return false
  }

  const counts = value.inventory

  if (
    !Object.hasOwn(counts, 'rpc') ||
    !Object.entries(counts).every(
      ([key, count]) =>
        inventoryKeys.includes(key) && typeof count === 'number' && Number.isSafeInteger(count) && count >= 0
    )
  ) {
    return false
  }

  if (value.status === 'unknown') {
    return value.inventory_complete === false && typeof value.reason === 'string' && value.reason.length > 0
  }

  if (value.inventory_complete !== true || 'reason' in value || !exactKeys(counts, inventoryKeys)) {
    return false
  }

  const busy = Object.values(counts).some(count => (count as number) > 0)

  return (value.status === 'idle' && !busy) || (value.status === 'busy' && busy)
}

/** Deliberately no inferred profile, shell shim, SSH, or legacy dashboard. */
export function ownerLaunchArgs(args: string[]): string[] | null {
  if (args.slice(0, 5).join('\0') !== ['-m', 'hermes_cli.main', '--profile', 'general', 'serve'].join('\0')) {
    return null
  }

  return ['-m', 'tui_gateway.owner_bootstrap', ...args.slice(2)]
}

/** Main-process only. Neither credential nor channel is attached to a renderer DTO. */
export function attachProcessOwner(child: ChildProcess, onClose: () => void = () => {}): ProcessOwner {
  const channel = child.stdio[3] as Duplex
  const key = randomBytes(32)
  let generation = ''
  let nonce: string | undefined
  let sequence = 0
  let closed = false
  let buffer = ''
  let pending: { resolve(value: unknown): void; reject(error: Error): void } | null = null
  let timer: ReturnType<typeof setTimeout> | undefined

  const fail = () => {
    if (closed) {
      return
    }

    closed = true
    key.fill(0)
    clearTimeout(timer)
    pending?.reject(new Error('Owner channel closed'))
    pending = null
    channel.destroy()
    onClose()
  }

  const read = (): Promise<unknown> =>
    new Promise((resolve, reject) => {
      pending = { resolve, reject }
      timer = setTimeout(fail, 5000)
    })

  channel.on('error', fail)
  channel.on('close', fail)
  child.once('exit', fail)
  channel.on('data', chunk => {
    buffer += chunk.toString()

    if (buffer.length > 4096) {
      return fail()
    }

    const end = buffer.indexOf('\n')

    if (end < 0) {
      return
    }

    try {
      const value = JSON.parse(buffer.slice(0, end))
      buffer = buffer.slice(end + 1)

      if (!pending || buffer.length) {
        return fail()
      }

      clearTimeout(timer)
      const receiver = pending
      pending = null
      receiver.resolve(value)
    } catch {
      fail()
    }
  })

  let queue = read().then(hello => {
    if (!isRecord(hello) || !exactKeys(hello, ['generation']) || !isProof(hello.generation)) {
      fail()
      throw new Error('Invalid owner handshake')
    }

    generation = hello.generation
  })

  // Mark bootstrap rejection handled even when no native controller requests yet.
  void queue.catch(fail)
  channel.write(JSON.stringify({ capability: key.toString('hex') }) + '\n')

  return {
    request<O extends keyof OwnerResponses>(operation: O): Promise<OwnerResponses[O]> {
      const result = queue.then(async () => {
        if (closed) {
          throw new Error('Owner channel closed')
        }

        if (!['status', 'quiesce', 'cancel'].includes(operation)) {
          throw new Error('Unsupported owner operation')
        }

        const seq = ++sequence
        const tag = createHmac('sha256', key).update(`${generation}:${seq}:${operation}`).digest('hex')
        const response = read()
        channel.write(JSON.stringify({ generation, seq, operation, tag }) + '\n')
        const value = await response

        if (!validResponse(operation, value, generation, nonce)) {
          fail()
          throw new Error('Invalid owner response')
        }

        if (operation === 'quiesce') {
          nonce = (value as ProcessReceipt).nonce
        }

        if (operation === 'cancel') {
          nonce = undefined
        }

        return value
      })

      queue = result.then(
        () => undefined,
        () => undefined
      )

      return result
    },
    close: fail
  }
}
