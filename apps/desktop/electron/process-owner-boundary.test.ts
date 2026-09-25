import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { expect, test } from 'vitest'

import { attachProcessOwner } from './process-owner'

const cases = [
  ['hello', 'None'],
  ['hello', '[]'],
  ['hello', '{"generation": 3}'],
  ['hello', '{"generation": ["a" * 64]}'],
  ['hello', '{"generation": "a" * 64, "extra": True}'],
  ['status', 'None'],
  ['status', '[]'],
  ['status', '{}'],
  ['status', '{"authority": "launch-channel", "process_fence": "true"}'],
  ['status', '{"error": "unauthorized"}'],
  ['cancel', '{"admission": "closed"}'],
  ['quiesce', '{"scope": "session", "status": "idle"}'],
  ['quiesce', 'dict(receipt, generation="b" * 64)'],
  ['quiesce', 'dict(receipt, nonce=None)'],
  ['quiesce', 'dict(receipt, inventory_complete=False)'],
  ['quiesce', 'dict(receipt, inventory={"rpc": 0})'],
  ['quiesce', 'dict(receipt, inventory=dict(counts, turns=1))'],
  ['quiesce', 'dict(receipt, inventory=dict(counts, rpc=-1))'],
  ['quiesce', 'dict(receipt, inventory=dict(counts, rpc=True))'],
  ['quiesce', 'dict(receipt, nonce="c" * 64)']
] as const

test.each(cases)(
  'Python boundary rejects and revokes malformed %s: %s',
  async (phase, payload) => {
    const home = mkdtempSync(path.join(os.tmpdir(), 'owner-invalid-'))

    const child = spawn(
      'python3',
      [
        '-c',
        `
import json, socket
s = socket.socket(fileno=3)
f = s.makefile('rwb', buffering=0)
def send(x): f.write(json.dumps(x).encode() + b'\\n')
def read(): return f.readline()
read()
counts = dict.fromkeys(['turns','children','delegations','processes','notifications','os_children','durable_notifications','rpc'], 0)
receipt = dict(scope='process',status='idle',admission='closed',generation='a'*64,nonce='d'*64,inventory=counts,inventory_complete=True)
phase = ${JSON.stringify(phase)}
send(${phase === 'hello' ? payload : '{"generation": "a" * 64}'})
if phase != 'hello':
    read()
    ${phase === 'quiesce' ? 'send(receipt); read()' : 'pass'}
    send(${payload})
if phase == 'hello' and read():
    send({'authority': 'launch-channel', 'process_fence': True})
while read(): pass
`
      ],
      { env: { PATH: process.env.PATH, HOME: home, HERMES_HOME: home }, stdio: ['ignore', 'pipe', 'pipe', 'pipe'] }
    )

    const exited = new Promise(resolve => child.once('exit', resolve))
    let output = ''
    child.stdout!.on('data', chunk => {
      output += chunk
    })
    child.stderr!.on('data', chunk => {
      output += chunk
    })
    const owners = new WeakMap<object, ReturnType<typeof attachProcessOwner>>()
    let revoked = 0

    const owner = attachProcessOwner(child, () => {
      revoked++
      owners.delete(child)
    })

    owners.set(child, owner)

    try {
      if (phase === 'quiesce') {
        expect(await owner.request('quiesce')).toMatchObject({ status: 'idle' })
      }

      const operation = phase === 'hello' ? 'status' : phase
      await expect(owner.request(operation)).rejects.toThrow()
      await expect(owner.request('status')).rejects.toThrow('Owner channel closed')
      expect(owners.has(child)).toBe(false)
    } finally {
      owner.close()
      await exited
      rmSync(home, { recursive: true, force: true })
    }

    expect(output).toBe('')
    expect(revoked).toBe(1)
  },
  10000
)
