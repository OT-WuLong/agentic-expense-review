import { defineStore } from 'pinia'

import {
  fetchApproval,
  fetchApprovals,
  fetchAudit,
  retryApproval,
  submitHumanReview,
} from '@/api/approvals'
import type { ApprovalDetail, ApprovalListItem, AuditEvent } from '@/types'

export const useApprovalStore = defineStore('approval', {
  state: () => ({
    activeRequestId: null as string | null,
    items: [] as ApprovalListItem[],
    detail: null as ApprovalDetail | null,
    audit: [] as AuditEvent[],
    total: 0,
    loading: false,
    listRequestId: 0,
    detailRequestId: 0,
    error: '',
  }),
  actions: {
    selectRequest(requestId: string) {
      this.detailRequestId++
      this.activeRequestId = requestId
      this.detail = null
      this.audit = []
      this.error = ''
    },
    leaveRequest(requestId: string) {
      if (this.activeRequestId !== requestId) return
      this.activeRequestId = null
      this.detail = null
      this.audit = []
    },
    mergeEvent(event: AuditEvent) {
      if (event.request_id !== this.activeRequestId) return
      if (this.audit.some((item) => item.event_id === event.event_id)) return
      this.audit.push(event)
      this.audit.sort((left, right) => left.event_id - right.event_id)
    },
    async loadList(params: Record<string, unknown> = {}) {
      const requestId = ++this.listRequestId
      this.loading = true
      this.error = ''
      try {
        const page = await fetchApprovals(params)
        if (requestId === this.listRequestId) {
          this.items = page.items
          this.total = page.total
        }
      } catch (error) {
        if (requestId === this.listRequestId) {
          this.error = error instanceof Error ? error.message : '无法加载审批列表'
        }
      } finally {
        if (requestId === this.listRequestId) this.loading = false
      }
    },
    async loadDetail(requestId: string) {
      const sequence = ++this.detailRequestId
      if (this.activeRequestId === requestId) this.error = ''
      try {
        const detail = await fetchApproval(requestId)
        if (this.activeRequestId === requestId && sequence === this.detailRequestId)
          this.detail = detail
      } catch (error) {
        if (this.activeRequestId === requestId && sequence === this.detailRequestId) {
          this.error = error instanceof Error ? error.message : '无法加载审批详情'
        }
        throw error
      }
    },
    async loadAudit(requestId: string) {
      const result = await fetchAudit(requestId)
      if (this.activeRequestId !== requestId) return
      const events = new Map(
        [...this.audit, ...result.items.filter((item) => item.request_id === requestId)]
          .map((item) => [item.event_id, item]),
      )
      this.audit = [...events.values()].sort((left, right) => left.event_id - right.event_id)
    },
    async review(requestId: string, payload: Parameters<typeof submitHumanReview>[1]) {
      await submitHumanReview(requestId, payload)
      await this.loadDetail(requestId)
    },
    async retry(requestId: string) {
      await retryApproval(requestId)
      await Promise.all([this.loadDetail(requestId), this.loadAudit(requestId)])
    },
  },
})
