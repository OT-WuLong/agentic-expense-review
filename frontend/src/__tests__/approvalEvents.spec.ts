import { defineComponent, h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useApprovalEvents } from '@/composables/useApprovalEvents'
import type { AuditEvent } from '@/types'

describe('approval SSE', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('reconnects once, parses the event, and persists Last-Event-ID', async () => {
    const events: AuditEvent[] = []
    const payload = {
      event_id: 9,
      request_id: 'REQ-SSE-1',
      event_type: 'APPROVAL_FINALIZED',
      actor: 'SYSTEM',
      payload: { status: 'COMPLETED' },
      created_at: '2026-09-22T00:00:00Z',
    }
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(
        new Response(`id: 9\nevent: APPROVAL_FINALIZED\ndata: ${JSON.stringify(payload)}\n\n`, {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        }),
      )
    vi.stubGlobal('fetch', fetchMock)
    const Harness = defineComponent({
      setup() {
        useApprovalEvents('REQ-SSE-1', (event) => {
          events.push(event)
        })
        return () => h('div')
      },
    })
    const wrapper = mount(Harness)

    await flushPromises()
    await vi.advanceTimersByTimeAsync(800)
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(events.map((item) => item.event_id)).toEqual([9])
    expect(sessionStorage.getItem('approval-event-cursor:REQ-SSE-1')).toBe('9')
    wrapper.unmount()
  })

  it('reconnects when a non-terminal stream closes cleanly', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response('', {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    let stream!: ReturnType<typeof useApprovalEvents>
    const Harness = defineComponent({
      setup() {
        stream = useApprovalEvents('REQ-SSE-CLEAN-EOF', () => undefined)
        return () => h('div')
      },
    })
    const wrapper = mount(Harness)

    await flushPromises()
    await vi.advanceTimersByTimeAsync(800)
    await flushPromises()

    expect(stream.finished.value).toBe(false)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
})
