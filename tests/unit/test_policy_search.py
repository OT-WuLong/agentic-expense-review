from datetime import date

from app.agents.contracts import ToolCall, ToolName
from app.models import EvidenceItem
from app.tools.policy_search import PolicySearchTool
from app.tools.registry import ToolExecutionContext, ToolRegistry, ToolSpec


class FakeIndex:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search(self, query: str, **kwargs):
        self.calls.append({"query": query, **kwargs})
        item = EvidenceItem(
            evidence_id="chunk-1",
            source_type="POLICY_DOCUMENT",
            document_id="POL-1",
            version="1.0",
            effective_from="2026-01-01",
            page=2,
            section="交通",
            excerpt="公务出租车需要票据。",
        )
        return [item], object()


def context() -> ToolExecutionContext:
    return ToolExecutionContext(
        requester_role="APPLICANT",
        department_id="DEPT-OPERATIONS",
        allowed_department_ids=["DEPT-OPERATIONS"],
        allowed_document_ids=["POL-1"],
        allowed_tools=[ToolName.POLICY_SEARCH],
        policy_catalog_snapshot_id="POLICY-CATALOG-REF-1.2.0",
        structured_data_snapshot_id="STRUCTURED-DATA-REF-1.2.0",
        policy_effective_at="2026-05-20",
        structured_as_of="2026-05-21",
        expense_type="交通",
    )


def test_policy_search_uses_server_scope_and_tracks_new_evidence() -> None:
    index = FakeIndex()
    registry = ToolRegistry(
        [ToolSpec(ToolName.POLICY_SEARCH, PolicySearchTool(index), 3, 3)]
    )
    call = ToolCall(
        tool_name="policy_search",
        query="出租车需要什么票据？",
        purpose="FIND_TAXI_RECEIPT_RULE",
        filters={"expense_type": "交通", "effective_at": "2026-05-20", "top_k": 20},
    )

    first = registry.execute(call, context())
    second = registry.execute(call, context(), known_evidence_ids=first.source_ids)

    assert first.error is None
    assert first.source_ids == first.new_evidence_ids == ["chunk-1"]
    assert second.call_hash == first.call_hash
    assert second.new_evidence_ids == []
    assert index.calls[0] == {
        "query": call.query,
        "department_id": "DEPT-OPERATIONS",
        "expense_type": "交通",
        "effective_at": date(2026, 5, 20),
        "allowed_document_ids": ["POL-1"],
        "catalog_snapshot_id": "POLICY-CATALOG-REF-1.2.0",
        "top_k": 6,  # read extra candidates for authority balancing; registry still returns at most 3
        "timeout_seconds": 3,
    }


def test_policy_search_rejects_application_fact_override_before_backend() -> None:
    index = FakeIndex()
    registry = ToolRegistry(
        [ToolSpec(ToolName.POLICY_SEARCH, PolicySearchTool(index), 3, 5)]
    )
    call = ToolCall(
        tool_name="policy_search",
        query="住宿标准",
        purpose="FIND_LIMIT",
        filters={"expense_type": "住宿", "effective_at": "2026-05-20"},
    )

    observation = registry.execute(call, context())

    assert observation.error == "AUTHORIZATION_SCOPE_MISMATCH"
    assert observation.items == []
    assert index.calls == []
