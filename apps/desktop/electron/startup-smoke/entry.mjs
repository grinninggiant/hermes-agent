// Disposable test entry only; never a production launch option.
import { app, session, globalShortcut, utilityProcess, shell, net as electronNet } from 'electron'
import { installGuards, nodePty, requestDecision, requestExpected, recoveryAccepted, captureRecovery } from './guards.mjs'
import * as ptyNamespace from './guards.mjs'
import fs from 'node:fs'
import path from 'node:path'
import childProcess from 'node:child_process'
import http from 'node:http'
import https from 'node:https'
import net from 'node:net'
import tls from 'node:tls'
import { syncBuiltinESMExports } from 'node:module'
import { createHash } from 'node:crypto'

const root = path.dirname(app.getAppPath())
const trace = (event, details = {}) => {
  fs.appendFileSync(path.join(root, 'trace.jsonl'), JSON.stringify({ event, ...details }) + '\n')
}
let passed = false
let violations = 0
for (const name of ['home', 'appData', 'userData', 'sessionData', 'temp', 'logs', 'crashDumps']) {
  const directory = path.join(root, name)
  fs.mkdirSync(directory, { recursive: true })
  app.setPath(name, directory)
}
const guardedAPIs = []
const forbidden = name => {
  guardedAPIs.push(name)
  return () => {
    violations++
    trace('forbidden-effect', { name })
    throw new Error(`Isolated smoke forbids ${name}`)
  }
}
// Install before importing any application code; native PTY is replaced in the test bundle.
installGuards({ childProcess, http, https, net, tls, process, globalThis, app,
  globalShortcut, utilityProcess, shell, electronNet, nodePty }, forbidden)
syncBuiltinESMExports()
trace('guards-installed', { guardedAPIs, scope: 'JavaScript entry points, not total OS-effect absence' })

const deadline = setTimeout(() => {
  trace('timeout')
  app.quit()
}, 30000)
app.on('quit', (_event, exitCode) => {
  clearTimeout(deadline)
  trace('quit', { exitCode, passed: passed && violations === 0, violations })
})
app.on('browser-window-created', (_event, window) => {
  const preferences = window.webContents.getLastWebPreferences()
  trace('window-created', { sandbox: preferences.sandbox, contextIsolation: preferences.contextIsolation,
    nodeIntegration: preferences.nodeIntegration })
  window.webContents.once('did-finish-load', async () => {
    try {
      if (process.argv.includes('--probe-network')) {
        // Reserved invalid name, no service to contact; onBeforeRequest must cancel first.
        await window.webContents.executeJavaScript(`fetch('https://unexpected.invalid/startup-smoke-probe').catch(() => null)`)
      }
      const { before, dom, png, aligned, attempts } = await captureRecovery(window.webContents)
      fs.writeFileSync(path.join(root, 'renderer.png'), png)
      trace('renderer-capture', { before, after: dom, aligned, attempts,
        stableCaptures: aligned ? 2 : 0, screenshot: 'renderer.png', sha256: createHash('sha256').update(png).digest('hex') })
      trace('renderer-loaded', dom)
      if (!aligned) trace('renderer-unsettled', { reason: 'No stable, unoccluded recovery capture within bounded wait' })
      passed = aligned && recoveryAccepted(dom) && dom.nodeUnavailable && preferences.sandbox === true &&
        preferences.contextIsolation === true && preferences.nodeIntegration === false && violations === 0
    } catch (error) {
      trace('renderer-error', { message: error.message })
    } finally {
      app.quit()
    }
  })
})
function guardSession(target) {
  // The sole expected stylesheet is still cancelled; every other request fails acceptance.
  target.webRequest.onBeforeRequest({ urls: ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*'] },
    (details, callback) => {
      trace('network-blocked', { url: details.url, expected: requestExpected(details), resourceType: details.resourceType, method: details.method })
      callback(requestDecision(details, name => { violations++; trace('forbidden-effect', { name }) }))
    })
}
app.on('session-created', guardSession)
app.whenReady().then(() => {
  trace('app-ready')
  guardSession(session.defaultSession)
})
try {
  if (process.argv.includes('--probe-tripwires')) {
    // Electron ready waits for ESM entry evaluation: do not top-level-await it.
    app.whenReady().then(async () => {
      // Invalid inputs also prevent host effects if a regression removes a tripwire.
      const invalid = Symbol('not a valid native argument')
      const probes = [
        ['net.Socket.connect', () => new net.Socket().connect(invalid)],
        ['net.Server.listen', () => new net.Server().listen(invalid)],
        ['child_process.ChildProcess.spawn', () => new childProcess.ChildProcess().spawn(invalid)],
        ['node-pty.spawn', () => ptyNamespace.spawn(invalid)],
        ['node-pty.fork', () => ptyNamespace.fork(invalid)],
        ['node-pty.createTerminal', () => ptyNamespace.createTerminal(invalid)],
        ['utilityProcess.fork', () => utilityProcess.fork(invalid)],
        ['shell.openExternal', () => shell.openExternal(invalid)],
        ['shell.openPath', () => shell.openPath(invalid)],
        ['electron.net.request', () => electronNet.request(invalid)],
        ['electron.net.fetch', () => electronNet.fetch(invalid)],
      ]
      for (const [name, invoke] of probes) {
        let message = ''
        try { await invoke() } catch (error) { message = error.message }
        if (message !== `Isolated smoke forbids ${name}`) throw new Error(`Tripwire probe missed: ${name}: ${message}`)
        trace('tripwire-probe', { name, blocked: true })
      }
      trace('tripwire-probes-complete', { count: probes.length })
      // Deliberate violations MUST remain failed acceptance; never reset the counter.
      app.quit()
    }).catch(error => {
      trace('probe-error', { message: error.stack })
      app.quit()
    })
  } else {
    await import('./dist/main.mjs')
  }
} catch (error) {
  trace('import-error', { message: error.stack })
  app.quit()
}
