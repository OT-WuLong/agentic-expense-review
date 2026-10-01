import { http } from './http'
import type { Page } from '@/types'

export interface PolicyItem {
  document_id: string
  title: string
  version: string
  expense_types?: string[]
  expense_type?: string
  effective_from: string
  effective_to?: string
  published_status?: string
  status?: string
  source: 'MANIFEST' | 'UPLOAD'
  department_ids?: string[]
}

export async function fetchPolicies() {
  // ponytail: load the small MVP catalog fully; switch to table pagination if it grows large.
  const first = (await http.get<Page<PolicyItem>>('/policies', {
    params: { page: 1, page_size: 100 },
  })).data
  const items = [...first.items]
  for (let page = 2; page <= first.pages; page++) {
    const next = (await http.get<Page<PolicyItem>>('/policies', {
      params: { page, page_size: 100 },
    })).data
    items.push(...next.items)
  }
  return { ...first, items }
}

export async function uploadPolicy(payload: {
  document_id: string
  title: string
  expense_type: string
  version: string
  effective_from: string
  file: File
}) {
  const body = new FormData()
  Object.entries(payload).forEach(([key, value]) => body.append(key, value))
  return (await http.post('/policies', body)).data
}
