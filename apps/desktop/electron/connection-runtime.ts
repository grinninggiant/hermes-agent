import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import type { ConnectionRegistry } from './connection-registry'

export interface RuntimeCoordinate {
  agentRoot: string
  python: string
  commit: string
  manifestSha256: string
}
export interface RuntimeSelection {
  generation: number
  coordinate: RuntimeCoordinate | null
}

export function normalizeRuntimeSelection(value: any): RuntimeSelection {
  if (!value || !Number.isSafeInteger(value.generation) || value.generation < 0 || !('coordinate' in value)) {
    throw new Error('Invalid runtime selection')
  }

  const c = value.coordinate

  if (
    c !== null &&
    (!c ||
      !/^[a-f0-9]{40}$/.test(c.commit) ||
      !/^[a-f0-9]{64}$/.test(c.manifestSha256) ||
      ![c.agentRoot, c.python].every(p => typeof p === 'string' && path.isAbsolute(p) && path.normalize(p) === p))
  ) {
    throw new Error('Invalid runtime coordinate')
  }

  return {
    generation: value.generation,
    coordinate:
      c === null
        ? null
        : {
            agentRoot: c.agentRoot,
            python: c.python,
            commit: c.commit,
            manifestSha256: c.manifestSha256
          }
  }
}

/** Trust pins come from native distribution provisioning, NEVER the save payload.
 * Matches agent_release_builder schema 2. No Python is executed during validation.
 */
export function validateRuntimeCoordinate(c: RuntimeCoordinate, trusted: ReadonlySet<string>): RuntimeCoordinate {
  normalizeRuntimeSelection({ generation: 0, coordinate: c })

  if (!trusted.has(c.manifestSha256)) {
    throw new Error('Release manifest is not trusted')
  }

  const release = path.dirname(c.agentRoot)
  const manifestPath = path.join(release, 'manifest.json')

  const seal = (name: string) => {
    const st = fs.lstatSync(name)

    if (st.isSymbolicLink() || st.mode & 0o222 || (process.getuid && st.uid !== process.getuid())) {
      throw new Error('Release is not owner-sealed')
    }
  }

  if (
    fs.realpathSync(release) !== release ||
    c.agentRoot !== path.join(release, 'source') ||
    c.python !== path.join(release, 'venv/bin/python')
  ) {
    throw new Error('Release coordinate identity mismatch')
  }

  for (const name of [release, c.agentRoot, manifestPath, path.join(release, 'venv'), path.join(release, 'venv/bin')]) {
    seal(name)
  }

  const bytes = fs.readFileSync(manifestPath)

  if (createHash('sha256').update(bytes).digest('hex') !== c.manifestSha256) {
    throw new Error('Manifest digest mismatch')
  }

  const m = JSON.parse(bytes.toString())

  if (m.schema !== 2 || m.status !== 'ready' || m.target !== release || m.commit !== c.commit || !m.source_files) {
    throw new Error('Manifest candidate identity mismatch')
  }

  const digest = (name: string) => createHash('sha256').update(fs.readFileSync(name)).digest('hex')

  if (
    typeof m.python_resolved !== 'string' ||
    typeof m.python_requested !== 'string' ||
    fs.realpathSync(c.python) !== m.python_resolved ||
    fs.realpathSync(m.python_requested) !== m.python_resolved ||
    digest(m.python_resolved) !== m.python_sha256
  ) {
    throw new Error('Manifest interpreter identity mismatch')
  }

  const inventory = (root: string, allowPythonLinks: boolean) => {
    const files: Record<string, unknown> = {}

    const walk = (dir: string) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const name = path.join(dir, entry.name)
        const key = path.relative(root, name).split(path.sep).join('/')

        if (entry.isSymbolicLink()) {
          const resolved = fs.realpathSync(name)

          if (!allowPythonLinks || resolved !== m.python_resolved) {
            throw new Error('Untrusted release symlink')
          }

          files[key] = { link: fs.readlinkSync(name), resolved, sha256: digest(name) }
        } else {
          seal(name)

          if (entry.isDirectory()) {
            files[key] = { directory: true }
            walk(name)
          } else if (entry.isFile()) {
            files[key] = { executable: Boolean(fs.statSync(name).mode & 0o111), sha256: digest(name) }
          } else {
            throw new Error('Unsupported release entry')
          }
        }
      }
    }

    walk(root)

    return files
  }

  const record = (value: unknown): value is Record<string, unknown> =>
    Boolean(value) && typeof value === 'object' && !Array.isArray(value)

  const equal = (a: any, b: any): boolean =>
    record(a) && record(b)
      ? Object.keys(a).length === Object.keys(b).length &&
        Object.keys(a).every(k => Object.hasOwn(b, k) && equal(a[k], b[k]))
      : a === b

  if (
    ![m.source_files, m.generated_source_files, m.venv_files].every(record) ||
    Object.keys(m.generated_source_files).some(k => Object.hasOwn(m.source_files, k)) ||
    !equal(inventory(c.agentRoot, false), { ...m.source_files, ...m.generated_source_files }) ||
    !equal(inventory(path.join(release, 'venv'), true), m.venv_files)
  ) {
    throw new Error('Release inventory integrity mismatch')
  }

  fs.accessSync(c.python, fs.constants.X_OK)

  return { ...c }
}

export function resolveConnectionRuntime(
  registry: ConnectionRegistry,
  connectionId: string,
  profile: string,
  trusted: ReadonlySet<string>
): RuntimeCoordinate | null {
  if (connectionId !== 'local' || profile !== 'general') {
    return null
  }

  const source = registry.connections.find(c => c.id === connectionId)

  if (!source || source.kind !== 'local') {
    throw new Error('Unsupported runtime scope')
  }

  const selection = source.generalRuntime

  return selection?.coordinate ? validateRuntimeCoordinate(selection.coordinate, trusted) : null
}

/** Synchronous native single-writer transaction: no await between read/CAS/write.
 * Admission fencing and owner drain must precede calling this; not exposed to IPC yet.
 * Rollback is the same CAS with the retained prior coordinate (including null).
 */
export function changeRuntimeSelection(
  store: { read(): ConnectionRegistry; write(value: ConnectionRegistry): void },
  connectionId: string,
  profile: string,
  expected: RuntimeSelection,
  coordinate: RuntimeCoordinate | null,
  validate: (coordinate: RuntimeCoordinate) => RuntimeCoordinate
): RuntimeSelection {
  const registry = store.read()
  const source = registry.connections.find(c => c.id === connectionId)

  if (connectionId !== 'local' || profile !== 'general' || source?.kind !== 'local') {
    throw new Error('Unsupported runtime scope')
  }

  const current = normalizeRuntimeSelection(source.generalRuntime ?? { generation: 0, coordinate: null })

  if (JSON.stringify(current) !== JSON.stringify(normalizeRuntimeSelection(expected))) {
    throw new Error('stale runtime generation/coordinate')
  }

  const next = normalizeRuntimeSelection({
    generation: current.generation + 1,
    coordinate: coordinate === null ? null : validate(coordinate)
  })

  store.write({
    ...registry,
    connections: registry.connections.map(c => (c === source ? { ...c, generalRuntime: next } : c))
  })

  return next
}
