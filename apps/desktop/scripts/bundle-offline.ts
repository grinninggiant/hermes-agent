import fs from 'node:fs'

import { runBundleOffline } from '../electron/bundle-offline'

const [file] = process.argv.slice(2)

if (!file || process.argv.length !== 3) {
  console.error('Usage: node bundle-offline.mjs REQUEST.json')
  process.exitCode = 2
} else {
  try {
    console.log(JSON.stringify(await runBundleOffline(JSON.parse(fs.readFileSync(file, 'utf8')))))
  } catch (error) {
    console.error((error as Error).message)
    process.exitCode = 1
  }
}
