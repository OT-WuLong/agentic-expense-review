from decimal import Decimal

import pytest

from app.agents.contracts import ToolCall, ToolName
from app.tools.registry import ToolExecutionContext, ToolRegistry, ToolSpec
from app.tools.structured_lookup import StructuredLookupTool
from tests.structured_record_store import StructuredRecordStore


def context(**updates) -> ToolExecutionContext:
    values = {
        "requester_role": "FINANCE_REVIEWER",
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
        "allowed_cities": [],
        "allowed_employee_ids": [],
        "allowed_invoice_numbers": [],
    }
    return ToolExecutionContext.model_validate({**values, **updates})


@pytest.mark.parametrize(
    ("query_type", "arguments", "context_updates", "expected_value", "expected_fixture"),
    [
        (
            "city_tier",
            {"city": "杭州市"},
            {
                "department_id": "DEPT-SYN-EAST-SALES",
                "allowed_department_ids": ["DEPT-SYN-EAST-SALES"],
                "structured_data_snapshot_id": "STRUCTURED-DATA-SYN-1.0.0",
                "policy_catalog_snapshot_id": "POLICY-CATALOG-SYN-1.1.0",
                "policy_effective_at": "2026-04-08",
                "structured_as_of": "2026-04-15",
                "expense_type": "住宿",
                "allowed_cities": ["杭州市"],
            },
            "A",
            "CITY-TIER-2026-01",
        ),
        (
            "employee_department",
            {"employee_id": "EMP-SYN-002"},
            {
                "department_id": "DEPT-SYN-EAST-SALES",
                "allowed_department_ids": ["DEPT-SYN-EAST-SALES"],
                "structured_data_snapshot_id": "STRUCTURED-DATA-P06-1.0.0",
                "policy_effective_at": "2026-04-08",
                "structured_as_of": "2026-04-15",
                "expense_type": "住宿",
                "allowed_employee_ids": ["EMP-SYN-002"],
            },
            "DEPT-SYN-EAST-SALES",
            "EMPLOYEE-DEPARTMENT-SYN-002",
        ),
        (
            "budget_status",
            {"period": "2026-05"},
            {},
            "AVAILABLE",
            "BUDGET-MKT-MEAL-MISMATCH",
        ),
        (
            "duplicate_invoice",
            {"invoice_number": "SYN-MKT-MEAL-002"},
            {"allowed_invoice_numbers": ["SYN-MKT-MEAL-002"]},
            False,
            "DUPLICATE-MKT-MEAL-MISMATCH",
        ),
    ],
)
def test_structured_lookup_supports_only_schema_bound_queries(
    query_type: str,
    arguments: dict,
    context_updates: dict,
    expected_value: str | bool,
    expected_fixture: str,
) -> None:
    registry = ToolRegistry(
        [
            ToolSpec(
                ToolName.STRUCTURED_LOOKUP,
                StructuredLookupTool(StructuredRecordStore()),
                1,
                1,
            )
        ]
    )
    call = ToolCall(
        tool_name="structured_lookup",
        query=query_type,
        query_type=query_type,
        purpose="READ_STRUCTURED_FACT",
        arguments=arguments,
    )

    observation = registry.execute(call, context(**context_updates))

    assert observation.error is None
    assert len(observation.items) == 1
    assert observation.items[0].value == expected_value
    assert observation.items[0].fixture_id == expected_fixture
    assert observation.items[0].document_id is None
    if query_type == "budget_status":
        assert observation.items[0].available_amount == Decimal("7800.00")


def test_structured_lookup_empty_result_is_not_a_system_error() -> None:
    registry = ToolRegistry(
        [ToolSpec(ToolName.STRUCTURED_LOOKUP, StructuredLookupTool(StructuredRecordStore()), 1, 1)]
    )
    call = ToolCall(
        tool_name="structured_lookup",
        query="不存在的城市",
        query_type="city_tier",
        purpose="LOOKUP_CITY_TIER",
        arguments={"city": "不存在的城市"},
    )

    observation = registry.execute(call, context(allowed_cities=["不存在的城市"]))

    assert observation.error is None
    assert observation.items == []
    assert observation.source_ids == []
