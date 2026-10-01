import { http } from './http'

export interface FormalRule {
  id: string
  rule_id: string
  rule_version: string
  expense_type: string | null
  rule_type: string
  parameters: Record<string, string | number>
  source_document_id: string | null
  effective_from: string
  effective_to: string | null
  enabled: boolean
}

export interface CandidateRule {
  candidate_id: number
  target_rule_id: string
  department_id: string
  expense_type: string
  summary: string
  parameters: Record<string, string | number>
  status: 'PENDING' | 'APPROVED' | 'REJECTED' | 'SUPERSEDED'
  reviewer_id: string | null
  source_document_id: string | null
  effective_from: string | null
  replay_cases: Array<Record<string, unknown>> | null
  published_at: string | null
}

export async function fetchRules() {
  return (
    await http.get<{ rules: FormalRule[]; candidates: CandidateRule[] }>('/rules')
  ).data
}

export async function approveCandidate(
  candidateId: number,
  payload: {
    source_document_id: string
    effective_from: string
    parameters: Record<string, string | number>
    replay_cases: Array<Record<string, unknown>>
  },
) {
  await http.post(`/rules/candidates/${candidateId}/approve`, payload)
}

export async function rejectCandidate(candidateId: number) {
  await http.post(`/rules/candidates/${candidateId}/reject`)
}

export async function publishCandidate(candidateId: number) {
  await http.post(`/rules/candidates/${candidateId}/publish`)
}

export async function rollbackCandidate(candidateId: number) {
  await http.post(`/rules/candidates/${candidateId}/rollback`)
}
