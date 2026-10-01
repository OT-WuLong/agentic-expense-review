import json
import operator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_type_hints

import pytest
from pydantic import TypeAdapter, ValidationError

from app.agents.contracts import AgentStep
from app.graph.state import ApprovalState, validate_state
from app.models import ApprovalInput, ApprovalStatus

STATE_ADAPTER = TypeAdapter(ApprovalState)
BASE_STATE: ApprovalState = {
    "request_id": "REQ-1",
    "trace_id": "TRACE-1",
    "status": ApprovalStatus.RUNNING,
    "agent_step_count": 0,
    "technical_retry_count": 0,
    "node_attempts": {},
    "retrieval_round_count": 0,
    "supervisor_challenge_count": 0,
    "no_progress_rounds": 0,
    "query_rewrite_count": 0,
    "tokens_used": 0,
    "max_agent_steps": 5,
    "max_retrieval_rounds": 3,
    "max_query_rewrites": 2,
    "token_budget": 1000,
    "deadline_at": datetime.now(UTC) + timedelta(seconds=20),
}
STEP = AgentStep(
    step_index=1,
    agent="SUPERVISOR",
    action="RETRIEVE",
    thought_summary="需要确认适用制度",
    latency_ms=5,
    tokens_used=12,
)


def test_state_restores_structured_data_and_separate_budgets() -> None:
    request = ApprovalInput(
        request_id="REQ-1",
        applicant={
            "employee_id": "EMP-SYN-001",
            "department_id": "DEPT-SYN-RD",
            "display_name": "合成员工甲",
        },
        application={
            "expense_type": "交通",
            "currency": "CNY",
            "amount": "68.00",
            "occurred_on": "2026-03-12",
            "submitted_on": "2026-03-13",
            "description": "合成通勤费",
        },
    )
    state: ApprovalState = {
        **BASE_STATE,
        "request_id": request.request_id,
        "trace_id": "TRACE-1",
        "status": ApprovalStatus.RUNNING,
        "applicant": request.applicant,
        "application": request.application,
        "documents": request.documents,
        "agent_trajectory": [STEP],
        "agent_step_count": 1,
        "technical_retry_count": 2,
        "node_attempts": {"execute_tools": 2},
        "retrieval_round_count": 1,
        "query_rewrite_count": 0,
        "tokens_used": 12,
    }
    restored = STATE_ADAPTER.validate_json(STATE_ADAPTER.dump_json(state))

    assert restored["application"].amount == request.application.amount
    assert restored["agent_trajectory"] == [STEP]
    assert restored["agent_step_count"] == 1
    assert restored["technical_retry_count"] == 2
    assert restored["retrieval_round_count"] == 1
    assert restored["query_rewrite_count"] == 0
    assert restored["tokens_used"] == 12
    assert restored["deadline_at"].tzinfo is not None


@pytest.mark.parametrize(
    "field",
    ["agent_step_count", "technical_retry_count", "retrieval_round_count", "query_rewrite_count", "tokens_used"],
)
def test_negative_usage_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        STATE_ADAPTER.validate_python({**BASE_STATE, field: -1})


def test_naive_deadline_and_private_state_fields_are_rejected() -> None:
    core = BASE_STATE
    with pytest.raises(ValidationError):
        STATE_ADAPTER.validate_python(
            {**core, "deadline_at": datetime.fromisoformat("2026-09-15T12:00:00")}
        )
    with pytest.raises(ValidationError):
        STATE_ADAPTER.validate_python({**core, "chain_of_thought": "不要持久化"})
    with pytest.raises(ValidationError):
        STATE_ADAPTER.validate_python({key: value for key, value in core.items() if key != "request_id"})
    with pytest.raises(ValidationError):
        STATE_ADAPTER.validate_python({**core, "request_id": "   "})
    with pytest.raises(ValidationError):
        STATE_ADAPTER.validate_python({key: value for key, value in core.items() if key != "deadline_at"})


def test_validate_state_enforces_the_contract_at_the_boundary() -> None:
    assert validate_state(BASE_STATE)["request_id"] == "REQ-1"
    with pytest.raises(ValidationError):
        validate_state({**BASE_STATE, "chain_of_thought": "不要持久化"})
    with pytest.raises(ValidationError):
        validate_state({**BASE_STATE, "tenant_id": "tenant-not-supported"})


def test_only_append_only_lists_have_reducers() -> None:
    fields = get_type_hints(ApprovalState, include_extras=True)
    for field in ("agent_trajectory", "evidence", "rule_results", "tool_observations"):
        reducer = fields[field].__metadata__[0]
        assert reducer is operator.add
        assert reducer([STEP], [STEP]) == [STEP, STEP]
    assert not hasattr(fields["open_questions"], "__metadata__")
    assert not hasattr(fields["retrieval_plan"], "__metadata__")


def test_technical_retry_does_not_create_an_agent_step() -> None:
    state = STATE_ADAPTER.validate_python({**BASE_STATE, "agent_step_count": 1})
    retried = STATE_ADAPTER.validate_python({**state, "technical_retry_count": 1})
    assert retried["agent_step_count"] == 1
    assert retried["technical_retry_count"] == 1


def test_golden_a_allows_zero_query_rewrites() -> None:
    cases = json.loads(
        (Path(__file__).parents[2] / "data" / "fixtures" / "golden_cases.json").read_text(
            encoding="utf-8"
        )
    )["cases"]
    limits = cases[0]["hard_limits"]
    state = STATE_ADAPTER.validate_python(
        {
            **BASE_STATE,
            "request_id": cases[0]["input"]["request_id"],
            "trace_id": "TRACE-GC-A",
            "max_agent_steps": limits["max_agent_steps"],
            "max_retrieval_rounds": limits["max_retrieval_rounds"],
            "max_query_rewrites": limits["max_query_rewrites"],
        }
    )
    assert state["max_query_rewrites"] == 0
