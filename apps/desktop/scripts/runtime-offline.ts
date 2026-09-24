import fs from 'node:fs'

import pins from '../assets/trusted-runtime-manifests.json' with { type: 'json' }
import { type OfflineRequest, runOffline } from '../electron/runtime-offline'

// Deliberately no Electron/main import, backend launch, default personal path,
// trust override, authentication handling or process termination operation.
const [requestFile] = process.argv.slice(2)

if (!requestFile || process.argv.length !== 3) {
  console.error('Usage: node --import tsx scripts/runtime-offline.ts REQUEST.json')
  process.exitCode = 2
} else {
  try {
    const request = JSON.parse(fs.readFileSync(requestFile, 'utf8')) as OfflineRequest

    if (!['activate', 'rollback', 'recover'].includes(request.action)) {
      throw new Error('Invalid action')
    }

    console.log(JSON.stringify(await runOffline(request, pins)))
  } catch (error) {
    console.error((error as Error).message)
    process.exitCode = 1
  }
}
