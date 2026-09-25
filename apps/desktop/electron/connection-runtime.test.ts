import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { expect, test } from 'vitest'

import { resolveProfileBackendRoute } from './connection-config'
import { normalizeConnectionInput, normalizeRegistry, parseStoredRegistry } from './connection-registry'
import { changeRuntimeSelection, resolveConnectionRuntime } from './connection-runtime'

test('exact local runtimes never share another profile backend, including scoped REST requests', () => {
  for (const [profile, primaryProfile] of [
    ['general', 'default'],
    ['default', 'general']
  ]) {
    for (const requestPath of [undefined, '/api/config', '/api/sessions', '/api/actions/job/status']) {
      const opts = { primaryProfile, exactLocalRuntime: true, requestPath, requestMethod: 'GET' }
      expect(resolveProfileBackendRoute(profile, opts).backend).toBe('pool')
      expect(resolveProfileBackendRoute(profile, { ...opts, exactLocalRuntime: false }).backend).toBe('primary')
      expect(resolveProfileBackendRoute(profile, { ...opts, globalRemote: true }).backend).toBe('primary')
    }
  }

  expect(resolveProfileBackendRoute('general', { primaryProfile: 'general', exactLocalRuntime: true }).backend).toBe(
    'primary'
  )
})

test('registry roundtrip preserves scoped selection; CAS prevents replay and rollback restores unset', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-runtime-'))
  const file = path.join(dir, 'connections.json')
  let registry = normalizeRegistry(null)

  const coordinate = {
    agentRoot: '/release/source',
    python: '/release/venv/bin/python',
    commit: 'a'.repeat(40),
    manifestSha256: 'b'.repeat(64)
  }

  const store = {
    read: () => normalizeRegistry(JSON.parse(fs.readFileSync(file, 'utf8'))),
    write: (value: typeof registry) => fs.writeFileSync(file, JSON.stringify(value))
  }

  store.write(registry)

  try {
    const prior = { generation: 0, coordinate: null }
    const next = changeRuntimeSelection(store, 'local', 'general', prior, coordinate, c => c)
    expect(store.read().connections[0].generalRuntime).toEqual(next)
    expect(() => changeRuntimeSelection(store, 'local', 'general', prior, coordinate, c => c)).toThrow(/stale/)
    expect(
      normalizeConnectionInput({ id: 'local', kind: 'local', label: 'Renamed' }, store.read()).generalRuntime
    ).toEqual(next)
    expect(() => changeRuntimeSelection(store, 'local', 'other', next, null, c => c)).toThrow(/scope/)
    expect(() => changeRuntimeSelection(store, 'remote', 'general', next, null, c => c)).toThrow(/scope/)
    expect(changeRuntimeSelection(store, 'local', 'general', next, null, c => c)).toEqual({
      generation: 2,
      coordinate: null
    })
    expect(() => changeRuntimeSelection(store, 'local', 'general', prior, null, c => c)).toThrow(/stale/)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('resolver validates pinned sealed manifest identity and leaves other scopes/defaults alone', () => {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-release-')))
  const release = path.join(dir, 'candidate')
  fs.mkdirSync(path.join(release, 'source'), { recursive: true })
  fs.mkdirSync(path.join(release, 'venv/bin'), { recursive: true })
  fs.writeFileSync(path.join(release, 'venv/bin/python'), 'fixture executable', { mode: 0o500 })

  const manifest = JSON.stringify({
    schema: 2,
    generated_source_files: {},
    venv_files: {
      bin: { directory: true },
      'bin/python': { executable: true, sha256: createHash('sha256').update('fixture executable').digest('hex') }
    },
    python_resolved: path.join(release, 'venv/bin/python'),
    python_sha256: createHash('sha256').update('fixture executable').digest('hex'),
    status: 'ready',
    target: release,
    python_requested: path.join(release, 'venv/bin/python'),
    commit: 'a'.repeat(40),
    source_files: {}
  })

  fs.writeFileSync(path.join(release, 'manifest.json'), manifest, { mode: 0o400 })

  const coordinate = {
    agentRoot: path.join(release, 'source'),
    python: path.join(release, 'venv/bin/python'),
    commit: 'a'.repeat(40),
    manifestSha256: createHash('sha256').update(manifest).digest('hex')
  }

  for (const name of ['source', 'venv/bin', 'venv', '']) {
    fs.chmodSync(path.join(release, name), 0o500)
  }

  const registry = normalizeRegistry(null)
  registry.connections[0].generalRuntime = { generation: 1, coordinate }
  const trusted = new Set([coordinate.manifestSha256])

  try {
    expect(resolveConnectionRuntime(registry, 'local', 'general', trusted)).toEqual(coordinate)
    expect(resolveConnectionRuntime(registry, 'local', 'other', trusted)).toBeNull()
    expect(resolveConnectionRuntime(registry, 'remote', 'general', trusted)).toBeNull()
    fs.chmodSync(coordinate.python, 0o700)
    fs.writeFileSync(coordinate.python, 'tampered executable')
    fs.chmodSync(coordinate.python, 0o500)
    expect(() => resolveConnectionRuntime(registry, 'local', 'general', trusted)).toThrow(/interpreter/)
    fs.chmodSync(coordinate.python, 0o700)
    fs.writeFileSync(coordinate.python, 'fixture executable')
    fs.chmodSync(coordinate.python, 0o500)
    expect(resolveConnectionRuntime(normalizeRegistry(null), 'local', 'general', trusted)).toBeNull()
    expect(() => resolveConnectionRuntime(registry, 'local', 'general', new Set())).toThrow(/trusted/)

    for (const patch of [
      { commit: 'c'.repeat(40) },
      { python: '/bin/sh' },
      { agentRoot: '/tmp/other' },
      { manifestSha256: 'd'.repeat(64) }
    ]) {
      registry.connections[0].generalRuntime = { generation: 1, coordinate: { ...coordinate, ...patch } }
      expect(() => resolveConnectionRuntime(registry, 'local', 'general', trusted)).toThrow()
    }
  } finally {
    for (const name of ['', 'source', 'venv', 'venv/bin']) {
      fs.chmodSync(path.join(release, name), 0o700)
    }

    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('malformed authoritative selection fails closed instead of manufacturing an unset local runtime', () => {
  const raw = {
    connections: [
      {
        id: 'local',
        kind: 'local',
        label: 'Device',
        generalRuntime: { generation: 2, coordinate: { commit: 'broken' } }
      }
    ]
  }

  expect(() => normalizeRegistry(raw)).toThrow(/runtime/i)
  expect(raw.connections[0].generalRuntime.generation).toBe(2)

  for (const text of ['{', '{}', 'null', JSON.stringify({ version: 2, ...raw })]) {
    expect(() => parseStoredRegistry(text)).toThrow()
  }
})

test('stored registry rejects ambiguous local identity before normalization can discard its selection', () => {
  const registry = normalizeRegistry(null)

  const local = {
    ...registry.connections[0],
    generalRuntime: {
      generation: 9,
      coordinate: {
        agentRoot: '/release/source',
        python: '/release/venv/bin/python',
        commit: 'a'.repeat(40),
        manifestSha256: 'b'.repeat(64)
      }
    }
  }

  for (const conflict of [
    { id: 'wrong', kind: 'local' },
    { kind: 'local' },
    { id: 'local', kind: 'local' },
    { id: 'local', kind: 'remote', url: 'https://example.com' },
    { id: ' local ', kind: 'cloud', url: 'https://example.com' },
    { id: 'local', kind: 'ssh', host: 'example.com' },
    { id: 'local', kind: 'unknown' }
  ]) {
    for (const connections of [
      [conflict, local],
      [local, conflict]
    ]) {
      expect(() => parseStoredRegistry(JSON.stringify({ ...registry, connections }))).toThrow(
        /Invalid stored connections registry/
      )
    }
  }

  expect(
    parseStoredRegistry(JSON.stringify({ ...registry, connections: [local] })).connections[0].generalRuntime
  ).toEqual(local.generalRuntime)
})

test('stored legacy registry without a runtime selection retains unrelated normalization', () => {
  const registry = normalizeRegistry(null)

  const parsed = parseStoredRegistry(
    JSON.stringify({
      ...registry,
      connections: [
        registry.connections[0],
        { id: 'remote', kind: 'remote', url: 'https://one.example' },
        { id: 'remote', kind: 'remote', url: 'https://two.example' },
        { id: 'unknown', kind: 'unknown' }
      ]
    })
  )

  expect(parsed.connections[0]).toEqual(registry.connections[0])
  expect(parsed.connections[0].generalRuntime).toBeUndefined()
  expect(parsed.connections.slice(1).map(c => c.url)).toEqual(['https://one.example', 'https://two.example'])
  expect(parsed.connections[1].id).not.toBe(parsed.connections[2].id)
  expect(parsed.quarantined?.[0].reason).toBe('entry-unrecognized-kind')
})

test('reviewed trust pins are carried by the existing packaged resources mechanism', () => {
  const desktop = path.resolve(import.meta.dirname, '..')
  const config = JSON.parse(fs.readFileSync(path.join(desktop, 'package.json'), 'utf8'))
  const resource = config.build.extraResources.find((r: any) => r.to === 'trusted-runtime-manifests.json')
  expect(resource).toBeDefined()
  const pins = JSON.parse(fs.readFileSync(path.join(desktop, resource.from), 'utf8'))
  expect(pins.length).toBeGreaterThan(0)

  for (const pin of pins) {
    expect(Object.keys(pin).sort()).toEqual(['commit', 'manifestSha256'])
    expect(pin.commit).toMatch(/^[a-f0-9]{40}$/)
    expect(pin.manifestSha256).toMatch(/^[a-f0-9]{64}$/)
  }
})
