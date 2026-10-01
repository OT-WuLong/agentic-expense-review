import { describe, expect, it, vi } from 'vitest'

import { http } from '@/api/http'
import { fetchPolicies } from '@/api/policies'

vi.mock('@/api/http', () => ({ http: { get: vi.fn<typeof http.get>() } }))

describe('policy catalog API', () => {
  it('loads every page instead of stopping at 100 policies', async () => {
    const first = Array.from({ length: 100 }, (_, index) => ({ document_id: `POL-${index}` }))
    vi.mocked(http.get)
      .mockResolvedValueOnce({ data: { items: first, page: 1, page_size: 100, total: 101, pages: 2 } })
      .mockResolvedValueOnce({ data: { items: [{ document_id: 'POL-100' }], page: 2, page_size: 100, total: 101, pages: 2 } })

    const result = await fetchPolicies()

    expect(result.items).toHaveLength(101)
    expect(http.get).toHaveBeenNthCalledWith(2, '/policies', { params: { page: 2, page_size: 100 } })
  })
})
