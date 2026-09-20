import { test } from 'node:test'
import assert from 'node:assert/strict'
import { installGuards, nativeWindows, requestDecision, recoveryAccepted } from './guards.mjs'

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
  const dom = {rootChildren:1, headings:["Hermes couldn't start"], errors:["Error invoking remote method 'hermes:connection': Error: Backend unavailable in isolated startup smoke"]}
  assert.equal(recoveryAccepted(dom), true)
  assert.equal(recoveryAccepted({...dom, errors:['Other error']}), false)
  assert.equal(recoveryAccepted({...dom, headings:['Loading…']}), false)
  assert.equal(recoveryAccepted({...dom, errors:[]}), false)
})
