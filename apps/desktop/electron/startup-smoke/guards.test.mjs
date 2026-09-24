import { test } from 'node:test'
import assert from 'node:assert/strict'
import { installGuards, nativeWindows, requestDecision, recoveryAccepted, nodePty } from './guards.mjs'
import * as ptyNamespace from './guards.mjs'
import { readRecoveryDOM, captureRecovery } from './guards.mjs'
import { JSDOM } from 'jsdom'

// Real DOM/style queries; inert layout/compositor boundaries (jsdom does not paint).
function visualFixture() {
  const page = new JSDOM(`<div id="root"><div data-glass-opaque>
    <h2>Hermes couldn't start</h2><p class="text-destructive">Error invoking remote method 'hermes:connection': Error: Backend unavailable in isolated startup smoke</p>
    </div><div id="startup">Starting Hermes...</div></div>`, { runScripts: 'outside-only' })
  const { window } = page
  const { document } = window
  Object.defineProperty(document, 'readyState', { value: 'complete' })
  const heading = document.querySelector('h2')
  const error = document.querySelector('.text-destructive')
  const recovery = heading.parentElement
  const state = { covered: null, painted: 'Starting Hermes...', frames: 0, offscreen: false, rounded: false }
  for (const [element, y] of [[heading, 50], [error, 100]]) {
    element.getBoundingClientRect = () => ({ x: 10, y, left: 10, top: y,
      right: 310, bottom: y + 20, width: 300, height: 20,
      ...(state.offscreen ? { left: -400, right: -100, x: -400 } : {}) })
    element.getClientRects = () => [element.getBoundingClientRect()]
  }
  // The real error element has padding and rounded corners; the text is inset.
  const createRange = document.createRange.bind(document)
  document.createRange = () => {
    const range = createRange()
    const select = range.selectNodeContents.bind(range)
    let element
    range.selectNodeContents = target => { element = target; select(target) }
    range.getClientRects = () => {
      const r = element.getBoundingClientRect()
      return [{ x: r.left + 12, y: r.top + 4, left: r.left + 12, top: r.top + 4,
        right: r.right - 12, bottom: r.bottom - 4, width: r.width - 24, height: r.height - 8 }]
    }
    return range
  }
  document.elementFromPoint = (x, y) => {
    const target = y < 90 ? heading : error
    if (state.rounded && target === error && (x < 22 || x > 298) && (y < 104 || y > 116)) return recovery
    return state.covered === 'all' || state.covered === target ? document.getElementById('startup') : target
  }
  window.requestAnimationFrame = callback => setImmediate(() => {
    state.frames++
    callback(state.frames)
    state.painted = 'recovery'
  })
  const webContents = {
    executeJavaScript: source => Promise.resolve(window.eval(source)),
    capturePage: async () => {
      const pixels = state.covered ? 'Starting Hermes...' : state.painted
      return { toPNG: () => Buffer.from(pixels), isEmpty: () => false }
    }
  }
  return { page, state, heading, error, recovery, webContents,
    read: () => window.eval(`(${readRecoveryDOM})()`) }
}

test('recovery must be in the viewport, opaque, and unoccluded at both heading and error', () => {
  const fixture = visualFixture()
  const { state, heading, error, recovery, read, page } = fixture
  try {
    assert.equal(recoveryAccepted(read()), true)
    state.rounded = true
    assert.equal(recoveryAccepted(read()), true, 'unpainted rounded panel corners do not occlude its inset text')
    for (const covered of ['all', heading, error]) {
      state.covered = covered
      assert.equal(recoveryAccepted(read()), false, 'laid-out text behind another layer is not visible recovery')
    }
    state.covered = null
    state.offscreen = true
    assert.equal(recoveryAccepted(read()), false)
    state.offscreen = false
    for (const style of ['opacity:0', 'visibility:hidden', 'display:none']) {
      recovery.style.cssText = style
      assert.equal(recoveryAccepted(read()), false, style)
    }
    recovery.style.cssText = ''
    assert.equal(recoveryAccepted(read()), true)
  } finally { page.window.close() }
})

test('capture waits for paint and rejects recovery changed during capture or permanently covered', async () => {
  const { state, webContents, page } = visualFixture()
  try {
    const evidence = await captureRecovery(webContents, { maxAttempts: 2 })
    assert.equal(evidence.aligned, true)
    assert.equal(evidence.image.toPNG().toString(), 'recovery', 'a pre-paint startup image must not pass')
    const capture = webContents.capturePage
    webContents.capturePage = async () => {
      state.covered = 'all'
      return capture()
    }
    const changed = await captureRecovery(webContents, { maxAttempts: 2 })
    assert.equal(changed.aligned, false, 'post-capture visibility must also pass')
    assert.equal(changed.image.toPNG().toString(), 'Starting Hermes...')
    const stuck = await captureRecovery(webContents, { maxAttempts: 2 })
    assert.equal(stuck.aligned, false)
    assert.equal(stuck.attempts, 2, 'a stuck overlay exhausts the bounded wait, never becomes success')
  } finally { page.window.close() }
})

test('node-pty ESM named and default imports reach the installed tripwire', () => {
  for (const method of ['spawn', 'fork', 'createTerminal']) {
    const original = nodePty[method]
    let hits = 0
    nodePty[method] = () => { hits++; throw new Error(`blocked node-pty.${method}`) }
    try {
      assert.throws(() => ptyNamespace[method](), new RegExp(`blocked node-pty.${method}`))
      assert.throws(() => ptyNamespace.default[method](), new RegExp(`blocked node-pty.${method}`))
      assert.equal(hits, 2)
    } finally { nodePty[method] = original }
  }
})

test('recovery acceptance requires a mounted complete document and no unrelated error', () => {
  const dom = { readyState: 'complete', rootChildren: 1,
    headings: ["Hermes couldn't start"],
    errors: ["Error invoking remote method 'hermes:connection': Error: Backend unavailable in isolated startup smoke"] }
  assert.equal(recoveryAccepted(dom), true)
  assert.equal(recoveryAccepted({ ...dom, rootChildren: 0 }), false)
  assert.equal(recoveryAccepted({ ...dom, readyState: 'loading' }), false)
  assert.equal(recoveryAccepted({ ...dom, errors: [...dom.errors, 'Unrelated startup failure'] }), false)
})

// Doubles ensure even RED never reaches the host. Exercise the real installer.
for (const target of ['nativeWindows.activeWindow', 'nativeWindows.openWindows',
  'nativeWindows.activeWindowSync', 'nativeWindows.openWindowsSync',
  'childProcess.spawn', 'childProcess.spawnSync', 'childProcess.exec', 'childProcess.execSync',
  'childProcess.execFile', 'childProcess.execFileSync', 'childProcess.fork', 'process.kill',
  'http.request', 'http.get', 'https.request', 'https.get', 'net.connect', 'net.createConnection',
  'tls.connect', 'globalThis.fetch', 'app.setAsDefaultProtocolClient', 'globalShortcut.register',
  'net.Socket.prototype.connect', 'net.Server.prototype.listen',
  'childProcess.ChildProcess.prototype.spawn', 'nodePty.spawn', 'utilityProcess.fork',
  'childProcess.ChildProcess.prototype.kill', 'nodePty.fork', 'nodePty.createTerminal',
  'shell.openExternal', 'shell.openPath', 'shell.showItemInFolder', 'electronNet.request', 'electronNet.fetch']) {
  test(`blocks and records ${target}`, () => {
    let effects = 0
    const apis = Object.fromEntries(['childProcess','http','https','net','tls','process','globalThis','app','globalShortcut','nodePty','utilityProcess','shell','electronNet'].map(k => [k, {}]))
    apis.nativeWindows = nativeWindows
    apis.net.Socket = {prototype:{}}
    apis.net.Server = {prototype:{}}
    apis.childProcess.ChildProcess = {prototype:{}}
    let owner = apis
    const parts = target.split('.')
    for (const key of parts.slice(0, -1)) owner = owner[key] ??= {}
    owner[parts.at(-1)] = () => { effects++ }
    const violations = []
    installGuards(apis, name => () => { violations.push(name); throw new Error('blocked') })
    assert.throws(() => owner[parts.at(-1)](), /blocked/)
    assert.equal(effects, 0)
    assert.equal(violations.length, 1)
  })
}
test('only the exact expected stylesheet is classified as expected; all requests are cancelled', () => {
  const font = 'https://fonts.googleapis.com/css2?family=Courier+Prime:wght@400;700&display=swap'
  const violations = []
  for (const details of [{url:font,resourceType:'stylesheet',method:'GET'},
    {url:font+'&extra=1',resourceType:'stylesheet',method:'GET'},
    {url:font,resourceType:'xhr',method:'GET'},
    {url:'http://127.0.0.1:8999/api',resourceType:'xhr',method:'GET'},
    {url:'wss://example.invalid/',resourceType:'webSocket',method:'GET'}]) {
    assert.deepEqual(requestDecision(details, v => violations.push(v)), {cancel:true})
  }
  assert.equal(violations.length, 4)
})
test('accepts actual recovery heading and exact backend error, rejects loading and unrelated errors', () => {
  const dom = {readyState:'complete', rootChildren:1, headings:["Hermes couldn't start"], errors:["Error invoking remote method 'hermes:connection': Error: Backend unavailable in isolated startup smoke"]}
  assert.equal(recoveryAccepted(dom), true)
  assert.equal(recoveryAccepted({...dom, errors:['Other error']}), false)
  assert.equal(recoveryAccepted({...dom, headings:['Loading…']}), false)
  assert.equal(recoveryAccepted({...dom, errors:[]}), false)
})
