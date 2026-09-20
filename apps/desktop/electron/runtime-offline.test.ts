import { spawn } from 'node:child_process'
import { execFileSync } from 'node:child_process'
import { once } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { expect, test } from 'vitest'

import { processStartMarker } from './backend-claim'
import { serializeBackendOwnership } from './backend-ownership'
import { inspectClosedDesktop } from './runtime-offline'

test('executable CLI refuses live installed process without registry reads', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'offline-cli-'))

  try {
    fs.writeFileSync(path.join(dir, 'backend-ownership.json'), '{"backends":[]}')
    const request = path.join(dir, 'request.json')
    fs.writeFileSync(
      request,
      JSON.stringify({
        action: 'recover',
        userData: dir,
        installedApp: process.execPath,
        expected: { generation: 0, coordinate: null }
      })
    )
    expect(() =>
      execFileSync(process.execPath, ['--import', 'tsx', path.resolve('scripts/runtime-offline.ts'), request], {
        stdio: 'pipe'
      })
    ).toThrow()
    expect(fs.existsSync(path.join(dir, 'connections.json'))).toBe(false)
    expect(fs.readdirSync(dir).sort()).toEqual(['backend-ownership.json', 'request.json'])
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('closed-app gate uses real process identity; live and unknown refuse, exited permits', async () => {
  const child = spawn(process.execPath, ['-e', 'process.stdin.resume()'], { stdio: ['pipe', 'ignore', 'ignore'] })
  await once(child, 'spawn')

  const entry = {
    pid: child.pid!,
    startMarker: await processStartMarker(child.pid!),
    nonce: 'fixture',
    profile: 'general'
  }

  const ownership = serializeBackendOwnership([entry])
  const scan = async () => []

  try {
    expect(
      await inspectClosedDesktop('/Applications/Hermes.app', '{"backends":[]}', async () => [
        { pid: 123, command: '/usr/bin/python3 -m hermes_cli.main serve --port 9120' }
      ])
    ).toBe('alive')
    expect(await inspectClosedDesktop('/Applications/Hermes.app', ownership, scan)).toBe('alive')
    expect(await inspectClosedDesktop('/Applications/Hermes.app', '{}', scan)).toBe('unknown')
    expect(
      await inspectClosedDesktop('/Applications/Hermes.app', ownership, async () => {
        throw new Error('denied')
      })
    ).toBe('unknown')
  } finally {
    child.stdin.end()
    await once(child, 'exit')
  }

  expect(await inspectClosedDesktop('/Applications/Hermes.app', ownership, scan)).toBe('absent')
})
