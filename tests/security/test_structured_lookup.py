import pytest
from pydantic import ValidationError

from app.agents.contracts import ToolCall, ToolName
from app.tools.registry import ToolExecutionContext, ToolRegistry, ToolSpec
from app.tools.structured_lookup import StructuredLookupTool
from tests.structured_record_store import StructuredRecordStore


def context(**updates) -> ToolExecutionContext:
    values = {
        "requester_role": "APPLICANT",
        "department_id": "DEPT-MARKETING",
        "allowed_department_ids": ["DEPT-MARKETING"],
        "allowed_document_ids": [],
        "allowed_tools": [ToolName.STRUCTURED_LOOKUP],
        "allowed_structured_query_types": [
            "city_tier",
            "employee_department",
            "budget_status",
            "duplicate_invoice",
        ],
        "policy_catalog_snapshot_id": "POLICY-CATALOG-REF-1.2.0",
        "structured_data_snapshot_id": "STRUCTURED-DATA-REF-1.2.0",
        "policy_effective_at": "2026-05-20",
        "structured_as_of": "2026-05-23",
        "expense_type": "餐饮",
        "allowed_cities": ["杭州市"],
        "allowed_employee_ids": ["EMP-TEST-01"],
        "allowed_invoice_numbers": ["SYN-MKT-MEAL-001"],
    }
    return ToolExecutionContext.model_validate({**values, **updates})


def registry() -> ToolRegistry:
    return ToolRegistry(
        [ToolSpec(ToolName.STRUCTURED_LOOKUP, StructuredLookupTool(StructuredRecordStore()), 1, 1)]
    )


def test_arbitrary_sql_and_unsupported_query_types_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolCall(
            tool_name="structured_lookup",
            query="任意查询",
            query_type="city_tier",
            purpose="BYPASS",
            arguments={"sql": "SELECT * FROM payroll"},
        )
    call = ToolCall(
        tool_name="structured_lookup",
        query="审批权限",
        query_type="approval_permission",
        purpose="UNSUPPORTED",
        arguments={"employee_id": "EMP-TEST-01"},
    )
    assert registry().execute(call, context()).error == "UNSUPPORTED_QUERY_TYPE"


@pytest.mark.parametrize(
    ("query_type", "arguments"),
    [
        ("city_tier", {"city": "上海市"}),
        ("employee_department", {"employee_id": "EMP-OTHER"}),
        ("budget_status", {"period": "2026-04"}),
        ("duplicate_invoice", {"invoice_number": "INVOICE-OTHER"}),
    ],
)
def test_structured_queries_enforce_server_scope(query_type: str, arguments: dict) -> None:
    call = ToolCall(
        tool_name="structured_lookup",
        query=query_type,
        query_type=query_type,
        purpose="READ_STRUCTURED_FACT",
        arguments=arguments,
    )

    observation = registry().execute(call, context())

    assert observation.error == "AUTHORIZATION_DENIED"
    assert observation.items == []


def test_wrong_structured_snapshot_returns_no_cross_snapshot_data() -> None:
    call = ToolCall(
        tool_name="structured_lookup",
        query="杭州城市等级",
        query_type="city_tier",
        purpose="LOOKUP_CITY_TIER",
        arguments={"city": "杭州市"},
    )

    observation = registry().execute(
        call, context(structured_data_snapshot_id="STRUCTURED-DATA-REF-1.2.0")
    )

    assert observation.error is None
    assert observation.items == []
