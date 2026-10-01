"""The deployed model budget is tunable but remains a hard ceiling."""

from app.agents.contracts import ToolName
from app.approval_service import ApprovalService
from app.graph.budget import hard_limit_violation_reason
from app.models import ApprovalInput


def test_configured_token_budget_is_still_enforced(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_TOKEN_BUDGET", "32000")
    approval = ApprovalInput.model_validate(
        {
            "request_id": "BUDGET-TEST",
            "applicant": {
                "employee_id": "EMP-TEST",
                "department_id": "DEPT-TEST",
                "display_name": "测试员工",
            },
            "application": {
                "expense_type": "住宿",
                "currency": "CNY",
                "amount": "100.00",
                "occurred_on": "2026-01-01",
                "submitted_on": "2026-01-02",
                "description": "测试行程",
            },
        }
    )
    state = ApprovalService._initial_state(
        approval,
        [],
        [],
        {"max_agent_steps": 8, "max_retrieval_rounds": 3, "max_query_rewrites": 2},
        [ToolName.POLICY_SEARCH],
    )
    assert state["token_budget"] == 32000
    state["tokens_used"] = 32001
    assert hard_limit_violation_reason(state) == "TOKEN_BUDGET"
