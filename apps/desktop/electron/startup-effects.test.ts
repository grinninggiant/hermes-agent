import { describe, expect, it, vi } from 'vitest'

import { startupEffects } from './startup-effects'
import { startupEffects as isolatedEffects } from './startup-smoke/effects'

describe('startup external effects', () => {
  it('production delegates once, preserving results, promises and failures', async () => {
    const value = { connected: true }
    const pending = Promise.resolve(value)
    const start = vi.fn(() => pending)
    expect(startupEffects.startBackend(start)).toBe(pending)
    expect(start).toHaveBeenCalledExactlyOnceWith()
    expect(startupEffects.checkForUpdates(() => pending)).toBe(pending)

    for (const effect of [startupEffects.stopBackend, startupEffects.registerProtocol,
      startupEffects.warmShell, startupEffects.recoverStartup, startupEffects.integrateDesktop]) {
      const operation = vi.fn(() => value)
      expect(effect(operation)).toBe(value)
      expect(operation).toHaveBeenCalledExactlyOnceWith()
      const failure = new Error('delegated failure')
      expect(() => effect(() => { throw failure })).toThrow(failure)
    }

    const reap = vi.fn(() => Promise.resolve())
    await startupEffects.reapBackends(reap)
    expect(reap).toHaveBeenCalledExactlyOnceWith()
  })

  it('isolated build never invokes supplied external operations', async () => {
    const forbidden = vi.fn(() => { throw new Error('external effect executed') })
    await expect(isolatedEffects.startBackend(forbidden)).rejects.toThrow('isolated startup smoke')
    await expect(isolatedEffects.checkForUpdates(forbidden)).rejects.toThrow('isolated startup smoke')
    await isolatedEffects.reapBackends(forbidden)
    isolatedEffects.stopBackend(forbidden)
    isolatedEffects.registerProtocol(forbidden)
    isolatedEffects.warmShell(forbidden)
    isolatedEffects.recoverStartup(forbidden)
    isolatedEffects.integrateDesktop(forbidden)
    expect(forbidden).not.toHaveBeenCalled()
  })
})
