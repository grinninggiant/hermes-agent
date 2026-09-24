import { expect, test, vi } from 'vitest'

test('registry publication fsyncs bytes before rename, then syncs the directory', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'registry-durability-'))
  const events: string[] = []

  const sync = fs.fsyncSync,
    rename = fs.renameSync

  vi.spyOn(fs, 'fsyncSync').mockImplementation(fd => {
    events.push('sync')
    sync(fd)
  })
  vi.spyOn(fs, 'renameSync').mockImplementation((a, b) => {
    events.push('rename')
    rename(a, b)
  })

  try {
    writeConnectionsRegistry(path.join(dir, 'connections.json'), normalizeRegistry(null))
    expect(events).toEqual(['sync', 'rename', 'sync'])
  } finally {
    vi.restoreAllMocks()
    fs.rmSync(dir, { recursive: true, force: true })
  }
})
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { normalizeRegistry } from './connection-registry'
import { writeConnectionsRegistry } from './connection-registry-store'

const release = process.env.OFFLINE_TEST_RELEASE
// Opt-in real sealed artifact, never a synthetic manifest or a modified trust pin.
test.skipIf(!release)(
  'actual pinned CLI activation, stale refusal, process crash and lease/journal recovery',
  () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'offline-cli-real-'))
    const pins = JSON.parse(fs.readFileSync('assets/trusted-runtime-manifests.json', 'utf8'))
    const candidate = { agentRoot: `${release}/source`, python: `${release}/venv/bin/python`, ...pins[0] }
    const file = path.join(dir, 'connections.json')
    const initial = { generation: 0, coordinate: null }
    const read = () => JSON.parse(fs.readFileSync(file, 'utf8')).connections[0].generalRuntime

    const invoke = (action: string, expected: unknown, crash = false, extra = {}) => {
      const request = path.join(dir, 'request.json')
      fs.writeFileSync(
        request,
        JSON.stringify({
          action,
          userData: dir,
          installedApp: '/Applications/Hermes.app',
          expected,
          ...(action === 'activate' ? { candidate } : {}),
          ...extra
        })
      )

      return spawnSync(
        process.execPath,
        [
          '--import',
          path.resolve('scripts/runtime-offline-boundary.fixture.mjs'),
          '--import',
          'tsx',
          path.resolve('scripts/runtime-offline.ts'),
          request
        ],
        {
          encoding: 'utf8',
          env: { ...process.env, HOME: dir, HERMES_HOME: dir, OFFLINE_TEST_CRASH: crash ? 'publication' : '' },
          timeout: 90000
        }
      )
    }

    try {
      fs.writeFileSync(path.join(dir, 'backend-ownership.json'), '{"backends":[]}')
      writeConnectionsRegistry(file, normalizeRegistry(null))
      const activated = invoke('activate', initial)
      expect(activated.stderr).toBe('')
      expect(activated.status).toBe(0)
      expect(read()).toEqual({ generation: 1, coordinate: candidate })
      expect(fs.existsSync(path.join(dir, 'runtime-rollback.json'))).toBe(true)
      expect(invoke('rollback', initial).status).toBe(1)
      expect(read().generation).toBe(1)
      expect(invoke('rollback', read()).status).toBe(0)
      expect(read()).toEqual({ generation: 2, coordinate: null })
      const crashed = invoke('activate', read(), true)
      expect(crashed.signal).toBe('SIGKILL')
      expect(read()).toEqual({ generation: 3, coordinate: candidate })
      expect(fs.lstatSync(path.join(dir, 'runtime-maintenance.lock')).isSymbolicLink()).toBe(true)
      expect(fs.existsSync(path.join(dir, 'runtime-transition.json'))).toBe(true)
      const recovered = invoke('recover', read())
      expect(recovered.stderr).toBe('')
      expect(recovered.status).toBe(0)
      expect(read()).toEqual({ generation: 4, coordinate: null })
      expect(fs.existsSync(path.join(dir, 'runtime-transition.json'))).toBe(false)
      expect(fs.readdirSync(dir)).not.toContain('runtime-maintenance.lock')
      expect(invoke('activate', read(), false, { pins: [{ ...pins[0] }] }).status).toBe(1)

      for (const patch of [
        { python: '/bin/sh' },
        { agentRoot: `${release}/source/../source` },
        { manifestSha256: '0'.repeat(64) }
      ]) {
        expect(invoke('activate', read(), false, { candidate: { ...candidate, ...patch } }).status).toBe(1)
        expect(read()).toEqual({ generation: 4, coordinate: null })
      }

      const ownership = path.join(dir, 'backend-ownership.json')
      fs.renameSync(ownership, path.join(dir, 'redirected.json'))
      fs.symlinkSync(path.join(dir, 'redirected.json'), ownership)
      expect(invoke('recover', read()).stderr).toMatch(/Unsafe maintenance metadata path/)
      console.log(
        'CLI evidence: activate=1; rollback=2/null; SIGKILL publication=3; recover=4/null; lease and journal cleared'
      )
    } finally {
      fs.rmSync(dir, { recursive: true, force: true })
    }
  },
  180000
)
