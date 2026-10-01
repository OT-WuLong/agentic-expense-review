import { http } from './http'
import type { ApprovalDetail, ApprovalListItem, AuditEvent, Page } from '@/types'

export async function fetchApprovals(params: Record<string, unknown> = {}) {
  return (await http.get<Page<ApprovalListItem>>('/approvals', { params })).data
}

export async function fetchApproval(requestId: string) {
  return (await http.get<ApprovalDetail>(`/approvals/${requestId}`)).data
}

export async function fetchAudit(requestId: string) {
  return (await http.get<{ items: AuditEvent[]; total: number }>(`/approvals/${requestId}/audit`))
    .data
}

export async function fetchFixtures() {
  return (
    await http.get<{
      items: Array<{
        case_id: string
        title: string
        expected_recommendation: string
      }>
    }>('/fixtures')
  ).data.items
}

export async function createFixture(fixtureCaseId: string, requestId: string) {
  return (
    await http.post(
      '/approvals',
      { fixture_case_id: fixtureCaseId, request_id: requestId },
      { headers: { 'Idempotency-Key': `create-${requestId}` }, timeout: 20_000 },
    )
  ).data as { request_id: string; status: string }
}

export async function createManualWithInvoice(request: Record<string, unknown>, invoice: File) {
  const requestId = String(request.request_id)
  const body = new FormData()
  body.append('request', JSON.stringify(request))
  body.append('invoice', invoice)
  return (
    await http.post(
      '/approvals/with-invoice',
      body,
      { headers: { 'Idempotency-Key': `create-${requestId}` }, timeout: 30_000 },
    )
  ).data as { request_id: string; status: string }
}

export async function submitHumanReview(
  requestId: string,
  payload: {
    action: 'APPROVE' | 'EDIT' | 'REJECT'
    reason: string
    recommendation?: string
    idempotency_key: string
  },
) {
  return (await http.post(`/approvals/${requestId}/review`, payload)).data
}

export async function retryApproval(requestId: string) {
  return (
    await http.post(
      `/approvals/${requestId}/retry`,
      {},
      { headers: { 'Idempotency-Key': `retry-${requestId}-${crypto.randomUUID()}` } },
    )
  ).data
}

export async function uploadApprovalDocument(
  requestId: string,
  documentId: string,
  documentType: string,
  file: File,
) {
  const body = new FormData()
  body.append('document_id', documentId)
  body.append('document_type', documentType)
  body.append('file', file)
  return (await http.post(`/approvals/${requestId}/documents`, body)).data
}
