import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { useSlashCommand } from './slash'

afterEach(cleanup)

it('routes typed commissioning and timeout recovery through the real desktop slash dispatcher', async () => {
  const receipt = { results: [{ task_index: 0, status: 'completed', acceptance: { reason: 'released' } }] }

  const requestGateway = vi.fn(async (method: string, params: unknown) => {
    const command = (params as { command?: string }).command

    if (method === 'slash.exec' && command === 'commission run') {
      throw new Error('request timed out after 30s: slash.exec')
    }

    if (method === 'command.dispatch') {
      throw new Error('not a quick/plugin/skill command')
    }

    return { output: JSON.stringify({ children: [], receipt }) }
  })

  const appendSessionTextMessage = vi.fn()
  const submitPromptText = vi.fn()

  const deps = {
    activeSessionIdRef: { current: 'owned-runtime' },
    selectedStoredSessionIdRef: { current: null },
    busyRef: { current: true },
    getRoutedStoredSessionId: () => null,
    getRuntimeIdForStoredSession: () => null,
    requestGateway,
    appendSessionTextMessage,
    submitPromptText,
    copy: {}
  } as unknown as Parameters<typeof useSlashCommand>[0]

  const { result } = renderHook(() => useSlashCommand(deps))
  await act(async () => {
    await result.current('/commission run')
  })
  expect(requestGateway).toHaveBeenCalledWith('slash.exec', {
    session_id: 'owned-runtime',
    command: 'commission run'
  })
  expect(appendSessionTextMessage.mock.calls.some(call => String(call[2]).includes('request timed out'))).toBe(true)
  await act(async () => {
    await result.current('/commission status')
  })
  expect(requestGateway).toHaveBeenCalledWith('slash.exec', {
    session_id: 'owned-runtime',
    command: 'commission status'
  })
  expect(appendSessionTextMessage.mock.calls.some(call => String(call[2]).includes(JSON.stringify(receipt)))).toBe(true)
  expect(submitPromptText).not.toHaveBeenCalled()
})
