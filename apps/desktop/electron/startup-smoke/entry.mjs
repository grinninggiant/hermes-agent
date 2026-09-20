// Disposable test entry only; never a production launch option.
import { app, session, globalShortcut, utilityProcess, shell, net as electronNet } from 'electron'
import { installGuards, nodePty, requestDecision, requestExpected, recoveryAccepted } from './guards.mjs'
import fs from 'node:fs'
import path from 'node:path'
import childProcess from 'node:child_process'
import http from 'node:http'
import https from 'node:https'
import net from 'node:net'
import tls from 'node:tls'
import { syncBuiltinESMExports } from 'node:module'

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
      await new Promise(resolve => setTimeout(resolve, 1000))
      const dom = await window.webContents.executeJavaScript(`({
        url: location.href, readyState: document.readyState,
        rootChildren: document.getElementById('root')?.childElementCount ?? 0,
        headings: [...document.querySelectorAll('h1,h2,h3,[role=heading]')].filter(e => e.getClientRects().length).map(e => e.textContent.trim()),
        errors: [...document.querySelectorAll('.text-destructive')].filter(e => e.getClientRects().length).map(e => e.textContent.trim()),
        nodeUnavailable: typeof require === 'undefined' && typeof process === 'undefined'
      })`)
      trace('renderer-loaded', dom)
      fs.writeFileSync(path.join(root, 'renderer.png'), (await window.webContents.capturePage()).toPNG())
      passed = recoveryAccepted(dom) && dom.nodeUnavailable && preferences.sandbox === true &&
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
  await import('./dist/main.mjs')
} catch (error) {
  trace('import-error', { message: error.stack })
  app.quit()
}
