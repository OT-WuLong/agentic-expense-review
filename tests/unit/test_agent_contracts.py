import pytest
from pydantic import ValidationError

from app.agents.contracts import (
    AgentStep,
    CoverageStatus,
    EvidenceReview,
    QuestionCoverage,
    RetrievalPlan,
    SubQuestion,
    SupervisorDecision,
    ToolCall,
    ToolObservation,
)
from app.agents.evidence_reviewer import _evidence_payload
from app.models import EvidenceItem

POLICY_EVIDENCE = EvidenceItem(
    evidence_id="EVID-POLICY-1",
    source_type="POLICY_DOCUMENT",
    document_id="POL-TRANSPORT-2026-V1",
    version="1.0",
    effective_from="2026-01-01",
    page=4,
    excerpt="日常通勤不可报销。",
)
POLICY_CALL = ToolCall(
    tool_name="policy_search",
    query="日常通勤是否允许报销",
    purpose="FIND_APPLICABLE_TRANSPORT_PROHIBITION",
    sub_question_id="Q-TRANSPORT-ELIGIBILITY",
    filters={"expense_type": "交通", "effective_at": "2026-03-12"},
)


def test_legal_agent_messages_restore_from_json() -> None:
    messages = [
        SupervisorDecision(action="RETRIEVE", reason="需要适用制度", retrieval_goal="交通制度"),
        RetrievalPlan(
            action="RETRIEVE",
            reason="检查通勤条款",
            sub_questions=[
                SubQuestion(
                    question_id="Q-TRANSPORT-ELIGIBILITY",
                    text="住所到固定办公室的通勤是否可报销？",
                )
            ],
            tool_calls=[POLICY_CALL],
        ),
        POLICY_CALL,
        ToolObservation(
            tool_name="policy_search",
            query=POLICY_CALL.query,
            items=[POLICY_EVIDENCE],
            source_ids=[POLICY_EVIDENCE.evidence_id],
            latency_ms=8,
            is_degraded=False,
        ),
        EvidenceReview(
            action="SUFFICIENT",
            reason="制度片段覆盖通勤规则",
            coverage=[
                QuestionCoverage(
                    question_id="Q-TRANSPORT-ELIGIBILITY",
                    status=CoverageStatus.SUPPORTED,
                    evidence_ids=[POLICY_EVIDENCE.evidence_id],
                )
            ],
        ),
        AgentStep(
            step_index=1,
            agent="RETRIEVAL",
            action="RETRIEVE",
            thought_summary="先找有效交通制度",
            tool_calls=[POLICY_CALL],
            new_evidence_ids=[POLICY_EVIDENCE.evidence_id],
            latency_ms=8,
        ),
    ]
    for message in messages:
        assert type(message).model_validate_json(message.model_dump_json()) == message


def test_policy_review_payload_keeps_meaningful_null_metadata() -> None:
    payload = _evidence_payload(POLICY_EVIDENCE)

    assert payload["effective_to"] is None
    assert payload["supersedes_document_id"] is None
    assert "score" not in payload


@pytest.mark.parametrize(
    ("contract", "payload"),
    [
        (SupervisorDecision, {"action": "PAY", "reason": "越权"}),
        (RetrievalPlan, {"action": "REQUEST_DOCUMENTS", "reason": "越权"}),
        (EvidenceReview, {"action": "APPROVE", "reason": "越权"}),
    ],
)
def test_unknown_or_other_role_actions_fail_closed(contract: type, payload: dict) -> None:
    with pytest.raises(ValidationError):
        contract.model_validate(payload)


def test_unknown_tool_and_command_arguments_fail_closed() -> None:
    call = POLICY_CALL.model_dump()
    with pytest.raises(ValidationError):
        ToolCall.model_validate({**call, "tool_name": "write_database"})
    with pytest.raises(ValidationError):
        ToolCall.model_validate({**call, "arguments": {"sql": "DELETE FROM approvals"}})
    with pytest.raises(ValidationError):
        ToolCall.model_validate({**call, "tool_name": "structured_lookup", "query_type": None})


@pytest.mark.parametrize(
    "scope_field",
    [
        "authorization_scope",
        "allowed_department_ids",
        "requester_role",
        "department_id",
        "department_ids",
    ],
)
@pytest.mark.parametrize("container", ["filters", "arguments"])
def test_agent_cannot_override_server_authorization_scope(scope_field: str, container: str) -> None:
    call = POLICY_CALL.model_dump()
    with pytest.raises(ValidationError):
        ToolCall.model_validate({**call, container: {scope_field: "unauthorized"}})


def test_supervisor_retrieval_requires_a_goal() -> None:
    with pytest.raises(ValidationError):
        SupervisorDecision(action="RETRIEVE", reason="没有交付目标")


def test_tool_call_must_reference_a_planned_sub_question() -> None:
    with pytest.raises(ValidationError):
        RetrievalPlan(
            action="RETRIEVE",
            reason="引用不存在的问题",
            sub_questions=[SubQuestion(question_id="Q-1", text="当前制度？")],
            tool_calls=[POLICY_CALL],
        )


@pytest.mark.parametrize("private_field", ["chain_of_thought", "private_reasoning", "analysis"])
def test_private_reasoning_fields_are_not_contract_fields(private_field: str) -> None:
    with pytest.raises(ValidationError):
        AgentStep.model_validate(
            {
                "step_index": 1,
                "agent": "SUPERVISOR",
                "action": "RETRIEVE",
                "thought_summary": "公开的简短决策理由",
                "latency_ms": 1,
                private_field: "私有思维链",
            }
        )


def test_reviewer_cannot_claim_sufficiency_without_citations() -> None:
    with pytest.raises(ValidationError):
        EvidenceReview(action="SUFFICIENT", reason="没有来源")
    with pytest.raises(ValidationError):
        QuestionCoverage(question_id="Q1", status="SUPPORTED", evidence_ids=[])
    with pytest.raises(ValidationError):
        QuestionCoverage(question_id="Q1", status="CONFLICTING", evidence_ids=["E1"])
    with pytest.raises(ValidationError):
        QuestionCoverage(question_id="Q1", status="CONFLICTING", evidence_ids=["E1", "E1"])


def test_stopping_plan_and_role_mismatch_reject_tools() -> None:
    with pytest.raises(ValidationError):
        RetrievalPlan(action="STOP", reason="已充分", tool_calls=[POLICY_CALL])
    with pytest.raises(ValidationError):
        AgentStep(
            step_index=1,
            agent="SUPERVISOR",
            action="SUFFICIENT",
            thought_summary="角色错误",
            latency_ms=0,
        )
    with pytest.raises(ValidationError):
        AgentStep(
            step_index=1,
            agent="SUPERVISOR",
            action="RETRIEVE",
            thought_summary="不应调用工具",
            tool_calls=[POLICY_CALL],
            latency_ms=0,
        )
    with pytest.raises(ValidationError):
        AgentStep(
            step_index=1,
            agent="RETRIEVAL",
            action="STOP",
            thought_summary="已经充分",
            tool_calls=[POLICY_CALL],
            latency_ms=0,
        )
    with pytest.raises(ValidationError):
        AgentStep(
            step_index=1,
            agent="RETRIEVAL",
            action="RETRIEVE",
            thought_summary="没有计划",
            latency_ms=0,
        )


def test_negative_tool_latency_is_invalid() -> None:
    with pytest.raises(ValidationError):
        ToolObservation(tool_name="policy_search", query="交通", latency_ms=-1)
