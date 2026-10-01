import { onBeforeUnmount, ref } from 'vue'

import { handleUnauthorized } from '@/api/http'
import type { AuditEvent } from '@/types'

export function useApprovalEvents(
  requestId: string,
  onEvent: (event: AuditEvent) => void | Promise<void>,
  options: { immediate?: boolean; isTerminal?: () => boolean } = {},
) {
  const connected = ref(false)
  const finished = ref(false)
  let reconnects = 0
  const error = ref('')
  let controller: AbortController | null = null
  let reconnectTimer = 0
  let stopped = false
  const cursorKey = `approval-event-cursor:${requestId}`
  let cursor = Number(sessionStorage.getItem(cursorKey) ?? 0)

  async function connect() {
    if (stopped) return
    finished.value = false
    controller = new AbortController()
    try {
      const response = await fetch(`/api/v1/approvals/${requestId}/events?after=${cursor}`, {
        headers: {
          Accept: 'text/event-stream',
          ...(cursor ? { 'Last-Event-ID': String(cursor) } : {}),
        },
        credentials: 'same-origin',
        signal: controller.signal,
      })
      if (response.status === 401) {
        stop()
        handleUnauthorized()
        return
      }
      if (!response.ok || !response.body) throw new Error(`SSE ${response.status}`)
      connected.value = true
      error.value = ''
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let terminal = false
      while (!stopped) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const frames = buffer.split('\n\n')
        buffer = frames.pop() ?? ''
        for (const frame of frames) {
          if (!frame.trim() || frame.startsWith(':')) continue
          let id = 0
          let data = ''
          for (const line of frame.split('\n')) {
            if (line.startsWith('id:')) id = Number(line.slice(3).trim())
            if (line.startsWith('data:')) data += line.slice(5).trim()
          }
          if (!data) continue
          const event = JSON.parse(data) as AuditEvent
          cursor = Math.max(cursor, id || event.event_id)
          sessionStorage.setItem(cursorKey, String(cursor))
          await onEvent(event)
          terminal ||= event.event_type === 'APPROVAL_FINALIZED'
        }
      }
      connected.value = false
      if (terminal || options.isTerminal?.()) finished.value = true
      else if (!stopped) scheduleReconnect()
    } catch (reason) {
      connected.value = false
      if (controller?.signal.aborted || stopped || finished.value) return
      error.value = reason instanceof Error ? reason.message : '实时连接中断'
      scheduleReconnect()
    }
  }

  function scheduleReconnect() {
    if (stopped || finished.value) return
    reconnects += 1
    window.clearTimeout(reconnectTimer)
    reconnectTimer = window.setTimeout(connect, Math.min(5000, 800 * reconnects))
  }

  function stop() {
    stopped = true
    connected.value = false
    window.clearTimeout(reconnectTimer)
    controller?.abort()
  }

  onBeforeUnmount(stop)
  if (options.immediate !== false) void connect()
  return { connected, finished, error, connect, stop }
}
