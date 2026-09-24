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
// Serialized into the sandboxed renderer; keep this function self-contained.
export function readRecoveryDOM() {
  const { document, innerWidth, innerHeight, location } = globalThis
  const inspect = e => {
    if (!e) return { visible: false, reason: 'missing' }
    const r = e.getBoundingClientRect()
    const bounds = { x: r.x, y: r.y, width: r.width, height: r.height }
    const hidden = reason => ({ visible: false, reason, bounds })
    if (!e.getClientRects().length || r.width <= 0 || r.height <= 0 ||
        r.left < 0 || r.top < 0 || r.right > innerWidth || r.bottom > innerHeight) return hidden('outside viewport')
    for (let owner = e; owner; owner = owner.parentElement) {
      const style = globalThis.getComputedStyle(owner)
      if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity || 1) < 1) return hidden('hidden or fading ancestor')
    }
    // Test the laid-out text, not the unpainted corners of its rounded container.
    // Layout and CSS visibility alone accept text underneath an opaque startup layer.
    const range = document.createRange()
    range.selectNodeContents(e)
    const textRects = [...range.getClientRects()].filter(rect => rect.width > 0 && rect.height > 0)
    if (!textRects.length) return hidden('no rendered text')
    for (const rect of textRects) {
      if (rect.left < 0 || rect.top < 0 || rect.right > innerWidth || rect.bottom > innerHeight) return hidden('text outside viewport')
      const dx = Math.min(1, rect.width / 4)
      const dy = Math.min(1, rect.height / 4)
      for (const x of [rect.left + dx, rect.left + rect.width / 2, rect.right - dx]) {
        for (const y of [rect.top + dy, rect.top + rect.height / 2, rect.bottom - dy]) {
          const top = document.elementFromPoint(x, y)
          if (!top || !e.contains(top)) return hidden(`occluded text by ${top?.tagName ?? 'nothing'}`)
        }
      }
    }
    return { visible: true, bounds,
      textBounds: textRects.map(({ x, y, width, height }) => ({ x, y, width, height })) }
  }
  const heading = [...document.querySelectorAll('h2')].find(e => e.textContent.trim() === "Hermes couldn't start")
  const recovery = heading?.closest('[data-glass-opaque]')
  const headingVisibility = inspect(heading)
  const errors = [...(recovery?.querySelectorAll('.text-destructive') ?? [])]
    .filter(e => e.textContent.trim()).map(e => ({ text: e.textContent.trim(), ...inspect(e) }))
  return {
    url: location.href, readyState: document.readyState,
    rootChildren: document.getElementById('root')?.childElementCount ?? 0,
    headings: recovery && headingVisibility.visible ? [heading.textContent.trim()] : [],
    errors: errors.filter(e => e.visible).map(e => e.text),
    visibility: { heading: headingVisibility, errors },
    nodeUnavailable: typeof require === 'undefined' && typeof process === 'undefined'
  }
}
export async function captureRecovery(webContents, { maxAttempts = 40 } = {}) {
  let evidence
  let previous
  for (let attempts = 1; attempts <= maxAttempts; attempts++) {
    // A DOM commit can precede Chromium's painted surface. Cross a paint boundary,
    // then bracket the capture with read-only observations; never dismiss an overlay.
    const before = await webContents.executeJavaScript(`new Promise(resolve =>
      requestAnimationFrame(() => requestAnimationFrame(() => resolve((${readRecoveryDOM})()))))`)
    const image = await webContents.capturePage()
    const dom = await webContents.executeJavaScript(`(${readRecoveryDOM})()`)
    const png = image.toPNG()
    const stable = recoveryAccepted(before) && recoveryAccepted(dom) && !image.isEmpty() &&
      JSON.stringify(before) === JSON.stringify(dom)
    // A single capture may still hold the previous compositor surface. Require two
    // identical, frame-separated captures with the same accepted DOM/geometry.
    const aligned = Boolean(stable && previous?.stable && previous.png.equals(png) &&
      JSON.stringify(previous.dom) === JSON.stringify(dom))
    evidence = { before, dom, image, png, aligned, attempts }
    if (aligned) return evidence
    previous = { dom, png, stable }
    if (attempts < maxAttempts) await new Promise(resolve => setTimeout(resolve, 250))
  }
  return evidence
}
export function recoveryAccepted(dom) {
  return dom.readyState === 'complete' && dom.rootChildren > 0 &&
    dom.headings.includes("Hermes couldn't start") && dom.errors.length === 1 &&
    dom.errors[0] === "Error invoking remote method 'hermes:connection': Error: Backend unavailable in isolated startup smoke"
}
