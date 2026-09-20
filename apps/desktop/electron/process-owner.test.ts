import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { expect, test } from 'vitest'

import { attachProcessOwner, ownerLaunchArgs } from './process-owner'

test('native private socket reaches Python authority without exporting secrets', async () => {
  const home = mkdtempSync(path.join(os.tmpdir(), 'owner-test-'))
  const root = path.resolve('../..')

  const child = spawn(
    'python3',
    [
      '-c',
      "from tui_gateway.owner_bootstrap import start_owner_channel; start_owner_channel(['--profile','general','serve']).join()"
    ],
    {
      cwd: root,
      env: { PATH: process.env.PATH, PYTHONPATH: root, HOME: home, HERMES_HOME: home },
      stdio: ['ignore', 'pipe', 'pipe', 'pipe']
    }
  )

  const exited = new Promise(resolve => child.once('exit', resolve))
  let output = ''
  child.stdout!.on('data', chunk => {
    output += chunk
  })
  child.stderr!.on('data', chunk => {
    output += chunk
  })
  const owner = attachProcessOwner(child)

  try {
    expect(await owner.request('status')).toEqual({ authority: 'launch-channel', process_fence: true })
    const receipt = await owner.request('quiesce')
    expect(receipt).toMatchObject({
      scope: 'process',
      status: 'unknown',
      admission: 'closed',
      inventory_complete: false,
      reason: 'inventory-unavailable'
    })
    expect(await owner.request('quiesce')).toEqual(receipt)
    expect(await owner.request('cancel')).toEqual({ admission: 'open' })
    const renewed = await owner.request('quiesce')
    expect(renewed.generation).toBe(receipt.generation)
    expect(renewed.nonce).not.toBe(receipt.nonce)
    expect(Object.keys(owner).sort()).toEqual(['close', 'request'])
    expect(ownerLaunchArgs(['-m', 'hermes_cli.main', '--profile', 'general', 'serve'])).toEqual([
      '-m',
      'tui_gateway.owner_bootstrap',
      '--profile',
      'general',
      'serve'
    ])
    expect(ownerLaunchArgs(['-m', 'hermes_cli.main', '--profile', 'other', 'serve'])).toBeNull()
    expect(ownerLaunchArgs(['-m', 'hermes_cli.main', 'serve'])).toBeNull()
  } finally {
    owner.close()
    await exited
    rmSync(home, { recursive: true, force: true })
  }

  expect(output).toBe('')
  await expect(owner.request('status')).rejects.toThrow('Owner channel closed')
}, 10000)

test('natural child exit revokes the native map entry without retaining a stale handle', async () => {
  const home = mkdtempSync(path.join(os.tmpdir(), 'owner-exit-'))

  const child = spawn(
    'python3',
    [
      '-c',
      `
import json, socket
s = socket.socket(fileno=3)
f = s.makefile('rwb', buffering=0)
f.readline()
f.write(json.dumps({'generation': 'a'*64}).encode() + bytes([10]))
f.readline()
f.write(json.dumps({'authority': 'launch-channel', 'process_fence': True}).encode() + bytes([10]))
`
    ],
    { env: { PATH: process.env.PATH, HOME: home, HERMES_HOME: home }, stdio: ['ignore', 'pipe', 'pipe', 'pipe'] }
  )

  const exited = new Promise(resolve => child.once('exit', resolve))
  const owners = new WeakMap<object, ReturnType<typeof attachProcessOwner>>()
  let revoked = 0

  const owner = attachProcessOwner(child, () => {
    revoked++
    owners.delete(child)
  })

  owners.set(child, owner)

  try {
    expect(await owner.request('status')).toEqual({ authority: 'launch-channel', process_fence: true })
    await exited
    expect(owners.has(child)).toBe(false)
    expect(revoked).toBe(1)
    await expect(owner.request('status')).rejects.toThrow('Owner channel closed')
  } finally {
    owner.close()
    await exited
    rmSync(home, { recursive: true, force: true })
  }
}, 10000)
