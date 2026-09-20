import { randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import { type ConnectionRegistry, parseStoredRegistry } from './connection-registry'
import { normalizeRuntimeSelection } from './connection-runtime'

/** Durable bundle fence, also used by General startup before resolving/spawning. */
export function assertBundleReady(root: string, ownedBinding?: string): void {
  const file = path.join(root, 'bundle-install.json')

  try {
    if (!fs.lstatSync(file).isFile()) {
      throw new Error('Unsafe bundle journal')
    }

    const value = JSON.parse(fs.readFileSync(file, 'utf8'))

    if (
      value.version !== 1 ||
      !value.binding ||
      value.binding.action !== 'install' ||
      value.binding.userData !== root ||
      !/^[a-zA-Z0-9-]{1,80}$/.test(value.binding.transaction ?? '') ||
      ![value.binding.oldSha256, value.binding.candidateSha256].every(
        v => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v)
      ) ||
      ![value.binding.oldCommit, value.binding.candidateCommit].every(
        v => typeof v === 'string' && /^[a-f0-9]{40}$/.test(v)
      ) ||
      ![value.binding.installedApp, value.binding.candidateApp].every(
        v => typeof v === 'string' && path.isAbsolute(v)
      ) ||
      Object.keys(value).sort().join() !== 'before,binding,phase,target,version' ||
      !value.before ||
      !value.target ||
      JSON.stringify(value.before) !== JSON.stringify(value.binding.expected)
    ) {
      throw new Error('Malformed bundle journal')
    }

    const before = normalizeRuntimeSelection(value.before)
    const target = normalizeRuntimeSelection(value.target)

    const expectedTarget = value.binding.runtimeCandidate
      ? normalizeRuntimeSelection({ generation: before.generation + 1, coordinate: value.binding.runtimeCandidate })
      : before

    if (JSON.stringify(target) !== JSON.stringify(expectedTarget)) {
      throw new Error('Malformed bundle target')
    }

    if (ownedBinding && JSON.stringify(value.binding) === ownedBinding) {
      return
    }

    if (!['complete', 'recovered'].includes(value.phase)) {
      throw new Error('Pending bundle recovery blocks start/write')
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
      return
    }

    throw new Error(`Bundle recovery fence: ${String(error)}`)
  }
}

/** Shared native persistence; never decrypts connection envelopes. */
export function writeConnectionsRegistry(file: string, registry: ConnectionRegistry, ownedBinding?: string): void {
  assertBundleReady(path.dirname(file), ownedBinding)
  fs.mkdirSync(path.dirname(file), { recursive: true })
  const temporary = `${file}.${randomUUID()}.tmp`
  const fd = fs.openSync(temporary, 'wx', 0o600)

  try {
    try {
      fs.writeFileSync(fd, JSON.stringify(registry, null, 2))
      fs.fsyncSync(fd)
    } finally {
      fs.closeSync(fd)
    }

    fs.renameSync(temporary, file)
    const dir = fs.openSync(path.dirname(file), 'r')

    try {
      fs.fsyncSync(dir)
    } finally {
      fs.closeSync(dir)
    }
  } finally {
    if (fs.existsSync(temporary)) {
      fs.unlinkSync(temporary)
    }
  }
}

export function readConnectionsRegistry(file: string): ConnectionRegistry {
  return parseStoredRegistry(fs.readFileSync(file, 'utf8'))
}
