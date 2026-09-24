// Fixture-only process/signature boundary, never imported by production CLI.
import './runtime-offline-boundary.test.mjs'
import cp from 'node:child_process'
import fs from 'node:fs'
import { syncBuiltinESMExports } from 'node:module'
const exec = cp.execFileSync
cp.execFileSync = function (command, args, options) {
  if (command === '/usr/bin/codesign') {
    if (process.env.BUNDLE_TEST_CRASH === 'signature-refused') throw Error('signature refused')
    return Buffer.from('')
  }
  if (command === '/usr/libexec/PlistBuddy') return 'com.nousresearch.hermes\n'
  return exec.call(this, command, args, options)
}
const rename = fs.renameSync
fs.renameSync = function (from, to) {
  if (String(to).endsWith('.displaced') && process.env.BUNDLE_TEST_CRASH === 'restore-refused') throw Error('restore refused')
  const result = rename.call(this, from, to)
  if (String(to).endsWith('/connections.json') && process.env.BUNDLE_TEST_CRASH === 'runtime-write') process.exit(86)
  if (String(to).endsWith('/bundle-install.json')) {
    const phase = JSON.parse(fs.readFileSync(to, 'utf8')).phase
    if (phase === process.env.BUNDLE_TEST_CRASH) process.exit(86)
  }
  return result
}
syncBuiltinESMExports()
