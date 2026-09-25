import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, expect, test, vi } from 'vitest'

import * as claim from './backend-claim'
import { inspectClosedDesktop } from './runtime-offline'

const home = os.homedir()
const python = '/reviewed/runtime/venv/bin/python'
const command = `${python} ${home}/.local/bin/hermes --profile general serve --isolated --host 127.0.0.1 --port 9120`
const row = { pid: 12345, command }
const target = `gui/${process.getuid?.()}/ai.hermes.serve-general`
const job = `${target} = {\n\tpath = ${home}/Library/LaunchAgents/ai.hermes.serve-general.plist\n\ttype = LaunchAgent\n\tstate = running\n\tprogram = ${home}/.hermes/scripts/hermes-serve-keychain.sh\n\truns = 19\n\tpid = ${row.pid}\n}`
const snapshot = `Wed Sep 16 16:14:21 2026 ${command}`

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllEnvs()
})

const realExecText = claim.execText

test.skipIf(process.platform !== 'darwin')(
  'external Terminal locales preserve SDK identity using real macOS start-time reads',
  async () => {
    for (const locale of ['C', 'tr_TR.UTF-8', 'de_DE.UTF-8']) {
      vi.restoreAllMocks()
      vi.stubEnv('LANG', locale)
      vi.stubEnv('LC_ALL', locale)
      vi.stubEnv('LC_TIME', locale)
      const osRead = boundary()
      // Only job/launcher/argv are fixtures. Exercise the actual ps date formatter
      // and execText environment propagation, without launching a service.
      osRead.mockImplementation(async (file, args, options) => {
        if (file === '/bin/launchctl') {
          return job
        }

        if (file === '/bin/ps' && args.includes('comm=')) {
          return python
        }

        if (file === '/bin/ps') {
          const start = await realExecText('/bin/ps', ['-p', String(process.pid), '-o', 'lstart='], options)

          return `${start} ${command}`
        }

        throw new Error('unexpected OS read')
      })
      expect(await inspectClosedDesktop('/Applications/Hermes.app', '{"backends":[]}', async () => [row]), locale).toBe(
        'absent'
      )
    }
  }
)

test.skipIf(process.platform !== 'darwin')(
  'denied OS identity reads remain blocking even with an exact launchd job',
  async () => {
    for (const code of ['EPERM', 'EACCES']) {
      vi.restoreAllMocks()
      boundary().mockImplementation(async file => {
        if (file === '/bin/launchctl') {
          return job
        }
        throw Object.assign(new Error('fixture permission denied'), { code })
      })
      expect(await inspectClosedDesktop('/Applications/Hermes.app', '{"backends":[]}', async () => [row])).toBe('alive')
    }
  }
)

function boundary(jobs = [job, job], snapshots = [snapshot, snapshot], executables = [python, python]) {
  const read = fs.readFileSync.bind(fs)
  vi.spyOn(fs, 'readFileSync').mockImplementation(((p: fs.PathOrFileDescriptor, ...args: unknown[]) =>
    p === path.join(home, '.local/bin/hermes')
      ? `#!${python}\n`
      : (read as Function)(p, ...args)) as typeof fs.readFileSync)

  return vi.spyOn(claim, 'execText').mockImplementation(async (file, args) => {
    if (file === '/bin/launchctl' && args.join(' ') === `print ${target}`) {
      return jobs.shift() ?? ''
    }

    if (file === '/bin/ps' && args.includes(String(row.pid))) {
      return args.includes('comm=') ? (executables.shift() ?? '') : (snapshots.shift() ?? '')
    }

    throw new Error('unexpected OS read')
  })
}

test.skipIf(process.platform !== 'darwin')(
  'stable exact independent General job is excluded, but Desktop evidence wins for every profile',
  async () => {
    boundary()
    expect(await inspectClosedDesktop('/Applications/Hermes.app', '{"backends":[]}', async () => [row])).toBe('absent')
    // No cached exemption on a later (including prepublication) inspection.
    expect(await inspectClosedDesktop('/Applications/Hermes.app', '{"backends":[]}', async () => [row])).toBe('alive')
    expect(
      await inspectClosedDesktop('/Applications/Hermes.app', '{"backends":[]}', async () => [
        row,
        { pid: 321, command: '/Applications/Hermes.app/Contents/MacOS/Hermes' }
      ])
    ).toBe('alive')
    vi.spyOn(claim, 'processStartMarker').mockResolvedValue('ps:live')

    for (const profile of ['general', 'other']) {
      const ownership = JSON.stringify({ backends: [{ ...row, profile, nonce: 'fixture', startMarker: 'ps:live' }] })
      expect(await inspectClosedDesktop('/Applications/Hermes.app', ownership, async () => [row])).toBe('alive')
    }

    vi.spyOn(claim, 'processStartMarker').mockResolvedValue('ps:reused')
    boundary()
    const conflict = JSON.stringify({
      backends: [{ ...row, profile: 'other', nonce: 'fixture', startMarker: 'ps:old' }]
    })
    expect(await inspectClosedDesktop('/Applications/Hermes.app', conflict, async () => [row])).toBe('alive')
    boundary()
    expect(
      await inspectClosedDesktop('/Applications/Hermes.app', '{"backends":[]}', async () => [
        row,
        { pid: 456, command: '/usr/bin/python3 -m hermes_cli.main serve --port 9120' }
      ])
    ).toBe('alive')
  }
)

test.skipIf(process.platform !== 'darwin')(
  'reuse, restart, stale, incomplete, conflicting and unknown evidence never excludes serve',
  async () => {
    const cases = [
      { jobs: [job, job.replace('runs = 19', 'runs = 20')] },
      { jobs: [job, job.replace('pid = 12345', 'pid = 12346')] },
      { snapshots: [snapshot, snapshot.replace('16:14:21', '16:14:22')] },
      { jobs: [job.replace('pid = 12345', 'pid = 12346')] },
      { jobs: [job.replace('\tprogram =', '\tunknown =')] },
      { jobs: [job.replace('state = running', 'state = waiting')] },
      { jobs: [job.replace('hermes-serve-keychain.sh', 'untrusted.sh')] },
      { jobs: [job.replace('\truns = 19', '\tpid = 999\n\truns = 19')] },
      { snapshots: [snapshot.replace(python, '/other/python')] },
      { executables: [python, '/other/python'] },
      { snapshots: [''] },
      { jobs: [''] }
    ]

    for (const c of cases) {
      vi.restoreAllMocks()
      boundary(c.jobs, c.snapshots, c.executables)
      expect(await inspectClosedDesktop('/Applications/Hermes.app', '{"backends":[]}', async () => [row])).not.toBe(
        'absent'
      )
    }

    vi.restoreAllMocks()
    boundary().mockRejectedValue(new Error('timeout'))
    expect(await inspectClosedDesktop('/Applications/Hermes.app', '{"backends":[]}', async () => [row])).not.toBe(
      'absent'
    )
    expect(await inspectClosedDesktop('/Applications/Hermes.app', '{}', async () => [row])).toBe('unknown')
  }
)
