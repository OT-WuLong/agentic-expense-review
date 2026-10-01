import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { fetchApproval, fetchApprovals } from '@/api/approvals'
import { useApprovalStore } from '@/stores/approval'
import type { ApprovalDetail, ApprovalListItem, AuditEvent, Page } from '@/types'

vi.mock('@/api/approvals', () => ({
  fetchApproval: vi.fn<() => Promise<ApprovalDetail>>(),
  fetchApprovals: vi.fn<() => Promise<Page<ApprovalListItem>>>(),
  fetchAudit: vi.fn<() => Promise<never>>(),
  submitHumanReview: vi.fn<() => Promise<void>>(),
}))

describe('approval store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('keeps the latest list when an earlier request completes later', async () => {
    const deferred = () => {
      let resolve!: (value: Page<ApprovalListItem>) => void
      const promise = new Promise<Page<ApprovalListItem>>((done) => {
        resolve = done
      })
      return { promise, resolve }
    }
    const older = deferred()
    const latest = deferred()
    vi.mocked(fetchApprovals).mockReturnValueOnce(older.promise).mockReturnValueOnce(latest.promise)
    const store = useApprovalStore()

    const oldLoad = store.loadList({ status: 'RUNNING' })
    const latestLoad = store.loadList({ status: 'COMPLETED' })
    latest.resolve({ items: [], page: 2, page_size: 50, total: 8, pages: 2 })
    await latestLoad
    older.resolve({ items: [], page: 1, page_size: 50, total: 20, pages: 1 })
    await oldLoad

    expect(store.total).toBe(8)
    expect(store.loading).toBe(false)
  })

  it('keeps the latest detail for the same request when responses arrive out of order', async () => {
    let resolveOlder!: (value: ApprovalDetail) => void
    const older = new Promise<ApprovalDetail>((resolve) => {
      resolveOlder = resolve
    })
    const snapshot = (status: 'RUNNING' | 'COMPLETED') =>
      ({ approval: { request_id: 'REQ-1', status } }) as ApprovalDetail
    vi.mocked(fetchApproval)
      .mockReturnValueOnce(older)
      .mockResolvedValueOnce(snapshot('COMPLETED'))
    const store = useApprovalStore()
    store.selectRequest('REQ-1')

    const oldLoad = store.loadDetail('REQ-1')
    const latestLoad = store.loadDetail('REQ-1')
    await latestLoad
    resolveOlder(snapshot('RUNNING'))
    await oldLoad

    expect(store.detail?.approval.status).toBe('COMPLETED')
  })

  it('merges SSE events by monotonically increasing event_id without duplicates', () => {
    const store = useApprovalStore()
    store.selectRequest('REQ-1')
    const event = (eventId: number): AuditEvent => ({
      event_id: eventId,
      request_id: 'REQ-1',
      event_type: 'WORKFLOW_NODE_COMPLETED',
      actor: 'SYSTEM',
      payload: {},
      created_at: '2026-09-22T00:00:00Z',
    })

    store.mergeEvent(event(2))
    store.mergeEvent(event(1))
    store.mergeEvent(event(2))

    expect(store.audit.map((item) => item.event_id)).toEqual([1, 2])
  })
})
