"""LangGraph shared state: typed fields, reducers, and an explicit boundary validator."""

import operator
from collections.abc import Mapping
from typing import Annotated, Required

from pydantic import AwareDatetime, ConfigDict, Field, TypeAdapter
from typing_extensions import TypedDict

from app.agents.contracts import (
    AgentStep,
    EvidenceReview,
    GuardrailDecision,
    RetrievalPlan,
    SubQuestion,
    SupervisorDecision,
    SupervisorDecisionRecord,
    ToolCall,
    ToolName,
    ToolObservation,
)
from app.models import (
    Applicant,
    Application,
    ApprovalResponse,
    ApprovalStatus,
    DecisionReason,
    Document,
    EvidenceItem,
    EvidenceMetrics,
    ExtractedField,
    FinalDecision,
    HumanAction,
    Recommendation,
    RiskLevel,
    RuleResult,
)

Count = Annotated[int, Field(ge=0, strict=True)]
Limit = Annotated[int, Field(gt=0, strict=True)]
Identity = Annotated[str, Field(min_length=1)]


# LangGraph 共享状态：节点间唯一数据总线；追加类字段用 operator.add reducer 合并
class ApprovalState(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra="forbid", str_strip_whitespace=True)  # type: ignore[valid-type]

    request_id: Required[Identity]
    trace_id: Required[Identity]
    status: Required[ApprovalStatus]
    applicant: Applicant
    application: Application
    documents: list[Document]
    allowed_tools: list[ToolName]
    policy_catalog_snapshot_id: str
    structured_data_snapshot_id: str
    extracted_fields: Annotated[list[ExtractedField], operator.add]
    extraction_issues: Annotated[list[str], operator.add]

    retrieval_goal: str
    supervisor_decision: SupervisorDecision | None
    supervisor_decisions: Annotated[list[SupervisorDecisionRecord], operator.add]
    supervisor_challenge_count: Required[Count]
    guardrail_decisions: Annotated[list[GuardrailDecision], operator.add]
    open_questions: list[SubQuestion]
    retrieval_plan: RetrievalPlan | None
    evidence_review: EvidenceReview | None
    selected_tools: list[ToolCall]
    query_variants: Annotated[list[str], operator.add]
    tool_observations: Annotated[list[ToolObservation], operator.add]
    retrieved_candidates: Annotated[list[EvidenceItem], operator.add]
    evidence: Annotated[list[EvidenceItem], operator.add]
    evidence_metrics: EvidenceMetrics

    agent_trajectory: Annotated[list[AgentStep], operator.add]
    agent_step_count: Required[Count]
    agent_stop_reason: str | None
    rule_results: Annotated[list[RuleResult], operator.add]
    risk_flags: Annotated[list[str], operator.add]
    risk_level: RiskLevel | None
    recommendation: Recommendation | None
    decision_reasons: list[DecisionReason]

    technical_retry_count: Required[Count]
    node_attempts: Required[dict[str, Count]]
    retrieval_round_count: Required[Count]
    no_progress_rounds: Required[Count]
    query_rewrite_count: Required[Count]
    tokens_used: Required[Count]
    max_agent_steps: Required[Limit]
    max_retrieval_rounds: Required[Limit]
    max_query_rewrites: Required[Count]
    token_budget: Required[Limit | None]
    deadline_at: Required[AwareDatetime]

    pending_human_action: HumanAction | None
    final_decision: FinalDecision | None
    human_review_idempotency_key: str | None


ApprovalStateAdapter = TypeAdapter(ApprovalState)


# 显式校验状态边界：LangGraph 只合并 TypedDict 注解，不会自动触发 Pydantic，需在入口/出口手动调用
def validate_state(state: Mapping[str, object]) -> ApprovalState:
    validated = ApprovalStateAdapter.validate_python(state)
    approval_response_from_state(validated)
    return validated


def approval_response_from_state(state: ApprovalState) -> ApprovalResponse:
    """Build the single validated public boundary used by evaluators and future APIs."""

    return ApprovalResponse(
        request_id=state["request_id"],
        trace_id=state["trace_id"],
        status=state["status"],
        recommendation=state.get("recommendation"),
        risk_level=state.get("risk_level"),
        reasons=state.get("decision_reasons", []),
        rule_results=state.get("rule_results", []),
        evidence=state.get("evidence", []),
        final_decision=state.get("final_decision"),
    )
