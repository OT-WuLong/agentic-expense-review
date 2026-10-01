import { beforeEach, describe, expect, it, vi } from 'vitest'

import { http, setCsrfToken } from '@/api/http'

describe('HTTP transport', () => {
  beforeEach(() => {
    setCsrfToken('')
    vi.restoreAllMocks()
  })

  it('sends a same-origin session and CSRF header for writes', async () => {
    setCsrfToken('csrf-demo')
    let seen: Record<string, unknown> = {}
    await http.post('/probe', {}, {
      adapter: async (config) => {
        seen = {
          csrf: config.headers.get('X-CSRF-Token'),
          requestId: config.headers.get('X-Request-ID'),
          timeout: config.timeout,
          withCredentials: config.withCredentials,
        }
        return { data: {}, status: 200, statusText: 'OK', headers: {}, config }
      },
    })

    expect(seen.csrf).toBe('csrf-demo')
    expect(seen.requestId).toEqual(expect.any(String))
    expect(seen.timeout).toBe(15_000)
    expect(seen.withCredentials).toBe(true)
  })

  it('maps the API error envelope to a readable Error message', async () => {
    await expect(
      http.get('/probe', {
        adapter: async () =>
          Promise.reject({
            message: 'Request failed',
            response: { data: { error: { message: '当前审批不在人工复核节点' } } },
          }),
      }),
    ).rejects.toMatchObject({ message: '当前审批不在人工复核节点' })
  })
})
