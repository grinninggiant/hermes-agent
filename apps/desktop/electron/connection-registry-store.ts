import { randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import { type ConnectionRegistry, parseStoredRegistry } from './connection-registry'

/** Shared native persistence; never decrypts connection envelopes. */
export function writeConnectionsRegistry(file: string, registry: ConnectionRegistry): void {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  const temporary = `${file}.${randomUUID()}.tmp`
  const fd = fs.openSync(temporary, 'wx', 0o600)

  try {
    try {
      fs.writeFileSync(fd, JSON.stringify(registry, null, 2))
      fs.fsyncSync(fd)
    } finally {
      fs.closeSync(fd)
    }

    fs.renameSync(temporary, file)
    const dir = fs.openSync(path.dirname(file), 'r')

    try {
      fs.fsyncSync(dir)
    } finally {
      fs.closeSync(dir)
    }
  } finally {
    if (fs.existsSync(temporary)) {
      fs.unlinkSync(temporary)
    }
  }
}

export function readConnectionsRegistry(file: string): ConnectionRegistry {
  return parseStoredRegistry(fs.readFileSync(file, 'utf8'))
}
