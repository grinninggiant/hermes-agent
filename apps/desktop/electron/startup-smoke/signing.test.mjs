import { test } from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const plist = input => JSON.parse(execFileSync('/usr/bin/python3', ['-I', '-c',
  'import json, plistlib, sys; print(json.dumps(plistlib.loads(sys.stdin.buffer.read())))'], { input, encoding: 'utf8' }))

// Read-only assertions can also reproduce the regression against a prior smoke copy.
export function assertDeclaredEntitlements(copy) {
  execFileSync('/usr/bin/codesign', ['--verify', '--deep', '--strict', copy])
  const frameworks = path.join(copy, 'Contents/Frameworks')
  const helpers = readdirSync(frameworks).filter(name => name.endsWith('.app') && name.includes('Helper'))
  assert.ok(helpers.length > 0, 'must inspect actual Electron helpers')
  for (const bundle of [copy, ...helpers.map(name => path.join(frameworks, name))]) {
    const declared = path.join(desktop, 'electron', bundle === copy ? 'entitlements.mac.plist' : 'entitlements.mac.inherit.plist')
    const actual = execFileSync('/usr/bin/codesign', ['-d', '--entitlements', '-', '--xml', bundle])
    assert.ok(actual.length > 0, `${bundle}: signature must contain declared entitlements`)
    assert.deepEqual(plist(actual), plist(readFileSync(declared)), bundle)
  }
}

test('signed smoke copy preserves declared main/helper entitlements and source inventory', {
  skip: process.platform !== 'darwin',
}, () => {
  const output = execFileSync(process.execPath, ['scripts/startup-smoke.mjs', '--signed-copy', '--build-only'], {
    cwd: desktop, encoding: 'utf8',
  })
  const root = output.match(/^TEST_BUNDLE=(.+)$/m)?.[1]
  assert.ok(root, output)
  const receipt = JSON.parse(readFileSync(path.join(root, 'signature.json'), 'utf8'))
  assertDeclaredEntitlements(path.join(root, 'Electron.app'))
  assert.equal(receipt.kind, 'ad-hoc')
  assert.equal(receipt.verified, true)
  assert.equal(receipt.productionAcceptance, false)
  assert.deepEqual(JSON.parse(readFileSync(path.join(root, 'source-before.json'))),
    JSON.parse(readFileSync(path.join(root, 'copy-before.json'))))
  assert.deepEqual(JSON.parse(readFileSync(path.join(root, 'source-before.json'))),
    JSON.parse(readFileSync(path.join(root, 'source-after.json'))))
})
