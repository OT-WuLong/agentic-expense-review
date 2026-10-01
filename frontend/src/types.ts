export type ApprovalStatus =
  | 'CREATED'
  | 'RUNNING'
  | 'BUSINESS_REJECTED'
  | 'INSUFFICIENT_EVIDENCE'
  | 'SYSTEM_ERROR'
  | 'HUMAN_PENDING'
  | 'COMPLETED'

export type Recommendation = 'PASS_RECOMMENDED' | 'REJECT_RECOMMENDED' | 'HUMAN_REVIEW'
export type HumanReviewAction = 'APPROVE' | 'EDIT' | 'REJECT'

export interface ApprovalListItem {
  request_id: string
  employee_id: string
  department_id: string
  expense_type: string
  currency: string
  amount: string
  occurred_on: string
  status: ApprovalStatus
  recommendation: Recommendation | null
  risk_level: 'LOW' | 'MEDIUM' | 'HIGH' | null
  updated_at: string
}

export interface EvidenceItem {
  evidence_id: string
  source_type: string
  document_id?: string
  version?: string
  page?: number
  section?: string
  excerpt?: string
  score?: number
  query_type?: string
  record_key?: string
  value?: string | number | boolean
  effective_from?: string
  effective_to?: string
}

export interface AgentStep {
  step_index: number
  agent: string
  action: string
  thought_summary: string
  latency_ms: number
  tokens_used: number
  tool_calls: Array<{
    tool_name: string
    query: string
    purpose: string
    call_id?: string
  }>
  observation_ids: string[]
  new_evidence_ids: string[]
  stop_reason?: string
}

export interface WorkflowState {
  trace_id: string
  status: ApprovalStatus
  recommendation: Recommendation | null
  risk_level: 'LOW' | 'MEDIUM' | 'HIGH' | null
  agent_stop_reason?: string
  agent_step_count: number
  retrieval_round_count: number
  query_rewrite_count: number
  tokens_used: number
  evidence: EvidenceItem[]
  agent_trajectory: AgentStep[]
  tool_observations: Array<{
    call_id?: string
    tool_name: string
    latency_ms: number
    error?: string
    is_degraded: boolean
    source_ids: string[]
  }>
  rule_results: Array<{
    rule_id: string
    rule_version: string
    outcome: 'PASS' | 'FAIL' | 'INDETERMINATE'
    reason_code?: string
    computed_limit?: string
    actual_amount?: string
    evidence_ids: string[]
  }>
  decision_reasons: Array<{
    code: string
    message: string
    evidence_ids: string[]
    rule_ids: string[]
  }>
  evidence_review?: {
    recommended_action: string
    reason: string
    evidence_gaps: string[]
    conflict_evidence_ids: string[]
  }
  pending_human_action?: {
    action: string
    reason: string
    missing_items: string[]
  }
  final_decision?: {
    action: string
    operator_id: string
    recommendation: Recommendation
    reason: string
  }
}

export interface ApprovalDetail {
  retry_mode: 'INVOICE_PARSE' | 'CHECKPOINT' | 'HUMAN_REVIEW' | 'INITIALIZE' | 'UNRECOVERABLE' | null
  approval: {
    request_id: string
    thread_id: string
    employee_id: string
    department_id: string
    expense_type: string
    currency: string
    amount: string
    occurred_on: string
    submitted_on: string
    application: Record<string, unknown>
    documents: Array<{
      document_id: string
      document_type: string
      media_type: string
      storage_uri?: string
      sha256?: string
      synthetic: boolean
    }>
    status: ApprovalStatus
    recommendation: Recommendation | null
    risk_level: 'LOW' | 'MEDIUM' | 'HIGH' | null
    final_decision?: Record<string, unknown>
    created_at: string
    updated_at: string
  }
  workflow: {
    state: WorkflowState
    next_nodes: string[]
    interrupted: boolean
    allowed_actions: HumanReviewAction[]
  } | null
  consistency: {
    status: 'CONSISTENT' | 'DIVERGED' | 'MISSING_CHECKPOINT'
    mismatched_fields: string[]
  }
}

export interface AuditEvent {
  event_id: number
  request_id: string
  event_type: string
  actor: string
  payload: Record<string, unknown>
  created_at: string
}

export interface Page<T> {
  items: T[]
  page: number
  page_size: number
  total: number
  pages: number
}
