import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { execText } from './backend-claim'

/** Ownership evidence for the existing General SDK launchd exec chain only.
 * Not an authorization override: no request-supplied labels, PIDs or exclusions.
 */
export async function isIndependentGeneralService(row: { pid: number; command: string }): Promise<boolean> {
  if (process.platform !== 'darwin' || !process.getuid) {
    return false
  }
  const home = os.homedir()
  const target = `gui/${process.getuid()}/ai.hermes.serve-general`
  const launcher = path.join(home, '.local/bin/hermes')

  const readPython = () => {
    const first = fs.readFileSync(launcher, 'utf8').split('\n')[0]
    const python = first.startsWith('#!') ? first.slice(2) : ''

    if (!path.isAbsolute(python) || /\s/.test(python)) {
      throw new Error('Unknown service interpreter')
    }

    return python
  }

  const readJob = async () => {
    const text = await execText('/bin/launchctl', ['print', target], { timeout: 5000 })

    if (!text.startsWith(`${target} = {\n`) || !text.endsWith('}')) {
      throw new Error('Unknown job')
    }

    // Only top-level fields count; nested environment/resource coalition PIDs
    // must never be mistaken for the job identity. Duplicate fields fail closed.
    const field = (key: string) => {
      const values = [...text.matchAll(new RegExp(`^\\t${key} = (.+)$`, 'gm'))]

      if (values.length !== 1) {
        throw new Error('Incomplete job identity')
      }

      return values[0][1]
    }

    const identity = {
      program: field('program'),
      pid: field('pid'),
      state: field('state'),
      type: field('type'),
      path: field('path'),
      runs: field('runs')
    }

    if (
      identity.program !== path.join(home, '.hermes/scripts/hermes-serve-keychain.sh') ||
      identity.path !== path.join(home, 'Library/LaunchAgents/ai.hermes.serve-general.plist') ||
      identity.type !== 'LaunchAgent' ||
      identity.state !== 'running' ||
      identity.pid !== String(row.pid) ||
      !/^[1-9]\d*$/.test(identity.runs)
    ) {
      throw new Error('Conflicting job identity')
    }

    return JSON.stringify(identity)
  }

  const readProcess = async (python: string) => {
    // lstart is locale-formatted (e.g. Turkish puts day before month).
    // Pin only this subprocess to the parser's format; identity checks stay exact.
    const text = await execText('/bin/ps', ['-ww', '-p', String(row.pid), '-o', 'lstart=,command='], {
      timeout: 5000,
      env: { ...process.env, LC_ALL: 'C' }
    })

    // macOS truncates comm when it is not the last column, even with -ww.
    const executable = await execText('/bin/ps', ['-ww', '-p', String(row.pid), '-o', 'comm='], { timeout: 5000 })
    const match = /^(\w{3} \w{3}\s+\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\s+(.+)$/.exec(text)

    if (!match || executable !== python || match[2] !== row.command) {
      throw new Error('Unknown process incarnation')
    }

    return JSON.stringify(match.slice(1))
  }

  try {
    const python = readPython()

    if (row.command !== `${python} ${launcher} --profile general serve --isolated --host 127.0.0.1 --port 9120`) {
      return false
    }
    const before = await readJob()
    const started = await readProcess(python)
    // No caching: every closed-app/prepublication inspection repeats all reads.
    const after = await readProcess(python)

    return before === (await readJob()) && started === after && python === readPython()
  } catch {
    return false
  }
}
