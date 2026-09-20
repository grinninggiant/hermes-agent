// Test-only JavaScript tripwires; not an OS sandbox or a filesystem confinement claim.
export const nodePty = {}
export const nativeWindows = {}
export default nodePty
// The bundle imports node-pty as an ESM namespace, not only its default object.
export const spawn = (...args) => nodePty.spawn(...args)
export const fork = (...args) => nodePty.fork(...args)
export const createTerminal = (...args) => nodePty.createTerminal(...args)
export function installGuards(apis, forbidden) {
  const { childProcess, http, https, net, tls, process, globalThis: globals, app, globalShortcut } = apis
  for (const name of ['spawn', 'spawnSync', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork']) childProcess[name] = forbidden(`child_process.${name}`)
  process.kill = forbidden('process.kill')
  for (const method of ['activeWindow', 'openWindows', 'activeWindowSync', 'openWindowsSync']) nativeWindows[method] = forbidden(`get-windows.${method}`)
  for (const [name, api, methods] of [
    ['http', http, ['request', 'get']], ['https', https, ['request', 'get']],
    ['net', net, ['connect', 'createConnection']], ['tls', tls, ['connect']]
  ]) for (const method of methods) api[method] = forbidden(`${name}.${method}`)
  for (const [name, api, methods] of [
    ['net.Socket', net.Socket.prototype, ['connect']],
    ['net.Server', net.Server.prototype, ['listen']],
    ['child_process.ChildProcess', childProcess.ChildProcess.prototype, ['spawn', 'kill']],
    ['node-pty', apis.nodePty, ['spawn', 'fork', 'createTerminal']],
    ['utilityProcess', apis.utilityProcess, ['fork']],
    ['shell', apis.shell, ['openExternal', 'openPath', 'showItemInFolder']],
    ['electron.net', apis.electronNet, ['request', 'fetch']]
  ]) for (const method of methods) api[method] = forbidden(`${name}.${method}`)
  globals.fetch = forbidden('fetch')
  app.setAsDefaultProtocolClient = forbidden('OS protocol registration')
  globalShortcut.register = forbidden('global shortcut registration')
}
export const expectedFont = 'https://fonts.googleapis.com/css2?family=Courier+Prime:wght@400;700&display=swap'
export function requestExpected(details) {
  return details.url === expectedFont && details.method === 'GET' && details.resourceType === 'stylesheet'
}
export function requestDecision(details, violation) {
  if (!requestExpected(details)) violation(`Chromium request: ${details.url}`)
  return { cancel: true }
}
export function recoveryAccepted(dom) {
  return dom.readyState === 'complete' && dom.rootChildren > 0 &&
    dom.headings.includes("Hermes couldn't start") && dom.errors.length === 1 &&
    dom.errors[0] === "Error invoking remote method 'hermes:connection': Error: Backend unavailable in isolated startup smoke"
}
