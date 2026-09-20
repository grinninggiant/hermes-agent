// Test-only preload: production has no inventory override. Only the exact
// inventory call is substituted; lease identity probes remain real OS calls.
import cp from 'node:child_process'
import fs from 'node:fs'
import { syncBuiltinESMExports } from 'node:module'
const exec = cp.execFile
cp.execFile = function (command, args, options, callback) {
  if (command === '/bin/ps' && JSON.stringify(args) === JSON.stringify(['-axo', 'pid=,command='])) {
    queueMicrotask(() => callback(null, `${process.pid} /test/isolated-node\n`))
    return
  }
  return exec.call(this, command, args, options, callback)
}
const rename = fs.renameSync
fs.renameSync = function (from, to) {
  const result = rename.call(this, from, to)
  if (process.env.OFFLINE_TEST_CRASH === 'publication' && String(to).endsWith('/connections.json'))
    process.kill(process.pid, 'SIGKILL')
  return result
}
syncBuiltinESMExports()
