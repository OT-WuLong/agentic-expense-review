from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.agents.contracts import ToolCall, ToolName, ToolObservation
from app.tools.registry import (
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
    UnknownToolError,
    tool_call_hash,
)


def context(**updates) -> ToolExecutionContext:
    values = {
        "requester_role": "APPLICANT",
        "department_id": "DEPT-OPERATIONS",
        "allowed_department_ids": ["DEPT-OPERATIONS"],
        "allowed_document_ids": ["POL-1"],
        "allowed_tools": [ToolName.POLICY_SEARCH],
        "policy_catalog_snapshot_id": "POLICY-CATALOG-REF-1.2.0",
        "structured_data_snapshot_id": "STRUCTURED-DATA-REF-1.2.0",
        "policy_effective_at": "2026-05-20",
        "structured_as_of": "2026-05-21",
        "expense_type": "交通",
    }
    return ToolExecutionContext.model_validate({**values, **updates})


def test_unknown_unregistered_and_unauthorized_tools_fail_closed() -> None:
    called = False

    def handler(call, execution_context, max_results, timeout_seconds):
        nonlocal called
        called = True
        return ToolObservation(tool_name=call.tool_name, query=call.query, latency_ms=0)

    registry = ToolRegistry(
        [ToolSpec(ToolName.STRUCTURED_LOOKUP, handler, 1, 1)]
    )
    with pytest.raises(UnknownToolError):
        registry.get("write_database")
    with pytest.raises(UnknownToolError):
        registry.get(ToolName.CASE_SEARCH)

    call = ToolCall(
        tool_name="structured_lookup",
        query="城市等级",
        query_type="city_tier",
        purpose="LOOKUP_CITY_TIER",
        arguments={"city": "杭州市"},
    )
    observation = registry.execute(call, context())
    assert observation.error == "TOOL_NOT_AUTHORIZED"
    assert called is False


def test_scope_injection_is_rejected_and_scope_changes_call_hash() -> None:
    base_call = ToolCall(
        tool_name="policy_search",
        query="  出租车\n票据  ",
        purpose="FIND_RULE",
        filters={"expense_type": "交通", "effective_at": "2026-05-20"},
    )
    with pytest.raises(ValidationError):
        ToolCall.model_validate(
            {**base_call.model_dump(), "filters": {"document_id": "POL-SECRET"}}
        )
    assert tool_call_hash(base_call, context()) == tool_call_hash(
        base_call.model_copy(update={"query": "出租车 票据"}), context()
    )
    later = context(
        policy_effective_at=context().policy_effective_at + timedelta(days=1),
        structured_as_of=context().structured_as_of + timedelta(days=1),
    )
    assert tool_call_hash(base_call, context()) != tool_call_hash(base_call, later)


def test_structured_call_hash_uses_only_effective_query_parameters() -> None:
    first = ToolCall(
        tool_name="structured_lookup",
        query="查询杭州等级",
        query_type="city_tier",
        purpose="LOOKUP_CITY_TIER",
        arguments={"city": "杭州市"},
    )
    second = first.model_copy(update={"query": "杭州市属于哪类城市"})

    assert tool_call_hash(first, context()) == tool_call_hash(second, context())


def test_timeout_becomes_a_controlled_observation() -> None:
    def timeout_handler(call, execution_context, max_results, timeout_seconds):
        raise TimeoutError

    registry = ToolRegistry(
        [ToolSpec(ToolName.POLICY_SEARCH, timeout_handler, 1, 1)]
    )
    call = ToolCall(
        tool_name="policy_search",
        query="交通制度",
        purpose="FIND_RULE",
        filters={"expense_type": "交通", "effective_at": "2026-05-20"},
    )

    observation = registry.execute(call, context())

    assert observation.error == "TOOL_TIMEOUT"
    assert observation.items == []
    assert observation.is_degraded is True
