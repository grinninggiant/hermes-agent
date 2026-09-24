// Build and run a TEST BUNDLE, never the installed/signed production candidate.
import { build } from 'esbuild'
import { mkdtempSync, mkdirSync, cpSync, writeFileSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn, execFileSync } from 'node:child_process'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const root = mkdtempSync(path.join(tmpdir(), 'hermes-startup-smoke-'))
const appRoot = path.join(root, 'app')
mkdirSync(appRoot)
for (const name of ['home', 'hermes-home', 'userData', 'temp']) mkdirSync(path.join(root, name))
writeFileSync(path.join(appRoot, 'package.json'), JSON.stringify({ name: 'hermes-isolated-startup-smoke', version: '0.0.0', main: 'entry.mjs', type: 'module' }))
cpSync(path.join(desktop, 'dist'), path.join(appRoot, 'dist'), { recursive: true,
  filter: source => !source.split(path.sep).includes('node_modules') && !source.endsWith('.node') })
for (const file of ['entry.mjs', 'guards.mjs']) cpSync(path.join(desktop, 'electron/startup-smoke', file), path.join(appRoot, file))
const bundle = await build({
  entryPoints: [path.join(desktop, 'electron/main.ts')], bundle: true, platform: 'node', format: 'esm',
  target: 'node20', outfile: path.join(appRoot, 'dist/main.mjs'), metafile: true,
  external: ['electron', 'fs'],
  banner: { js: "import { createRequire } from 'module'; const require = createRequire(import.meta.url);" },
  define: { 'process.env.HERMES_DESKTOP_IS_PACKAGED': 'true' },
  plugins: [{ name: 'isolated-startup-effects', setup(builder) {
    builder.onResolve({ filter: /^node-pty$/ }, () => ({ path: '../guards.mjs', external: true }))
    builder.onResolve({ filter: /^get-windows$/ }, () => ({ path: 'get-windows', namespace: 'blocked-native' }))
    builder.onLoad({ filter: /.*/, namespace: 'blocked-native' }, () => ({ contents: "import { nativeWindows } from '../guards.mjs'; export const activeWindow = (...args) => nativeWindows.activeWindow(...args); export const openWindows = (...args) => nativeWindows.openWindows(...args);", loader: 'js', resolveDir: appRoot }))
    builder.onResolve({ filter: /^\.\.\/guards\.mjs$/ }, () => ({ path: '../guards.mjs', external: true }))
    builder.onResolve({ filter: /^\.\/startup-effects$/ }, () => ({ path: path.join(desktop, 'electron/startup-smoke/effects.ts') }))
  } }]
})
writeFileSync(path.join(root, 'metafile.json'), JSON.stringify(bundle.metafile, null, 2))
if (Object.keys(bundle.metafile.inputs).some(p => p.endsWith('/electron/startup-effects.ts'))) throw new Error('Production effects included')
await build({ entryPoints: [path.join(desktop, 'electron/preload.ts')], bundle: true, platform: 'node',
  format: 'cjs', target: 'node20', outfile: path.join(appRoot, 'dist/electron-preload.js'), external: ['electron'] })
const env = { HOME: path.join(root, 'home'), HERMES_HOME: path.join(root, 'hermes-home'),
  HERMES_DESKTOP_USER_DATA_DIR: path.join(root, 'userData'), TMPDIR: path.join(root, 'temp'),
  PATH: '/usr/bin:/bin:/usr/sbin:/sbin', LANG: 'en_US.UTF-8' }
const probe = ['--probe-tripwires', '--probe-network'].filter(flag => process.argv.includes(flag))
if (probe.length > 1) throw new Error('Choose one negative probe at a time')
const args = [appRoot, `--user-data-dir=${env.HERMES_DESKTOP_USER_DATA_DIR}`, ...probe]
let executable = require('electron')
// Only sign a disposable dependency copy; never modify the dependency or installed app.
if (process.argv.includes('--signed-copy')) {
  if (process.platform !== 'darwin') throw new Error('--signed-copy requires macOS')
  const source = path.resolve(executable, '../../..')
  const copy = path.join(root, 'Electron.app')
  execFileSync('/usr/bin/python3', ['-I', path.join(desktop, 'electron/startup-smoke/sign-copy.py'),
    desktop, source, root], { env, stdio: 'pipe' })
  executable = path.join(copy, 'Contents/MacOS/Electron')
}
writeFileSync(path.join(root, 'launch.json'), JSON.stringify({ testBundle: true, productionAcceptance: false, executable, env, args }, null, 2))
console.log(`TEST_BUNDLE=${root}`)
if (process.argv.includes('--build-only')) process.exit(0)
const child = spawn(executable, args, { env, stdio: ['ignore', 'pipe', 'pipe'], cwd: root })
const output = []
child.stdout.on('data', data => output.push(data))
child.stderr.on('data', data => output.push(data))
// Termination is in the owned test entry via app.quit(), never a process sweep.
const exit = await new Promise((resolve, reject) => { child.once('exit', (code, signal) => resolve({ code, signal })); child.once('error', reject) })
writeFileSync(path.join(root, 'electron.log'), Buffer.concat(output))
writeFileSync(path.join(root, 'exit.json'), JSON.stringify(exit))
const events = readFileSync(path.join(root, 'trace.jsonl'), 'utf8')
console.log(events)
console.log(JSON.stringify(exit))
const quit = events.trim().split('\n').map(line => JSON.parse(line)).findLast(event => event.event === 'quit')
if (exit.code !== 0 || !quit?.passed || quit.violations !== 0) process.exitCode = 1
