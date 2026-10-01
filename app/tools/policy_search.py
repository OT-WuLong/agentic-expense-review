"""Agent-facing policy search with server-owned retrieval scope."""

from __future__ import annotations

from typing import Any

from pydantic import Field, ValidationError

from app.agents.contracts import ToolCall, ToolName, ToolObservation
from app.models import DateOnly, EvidenceItem, ExpenseType, StrictModel
from app.tools.registry import ToolExecutionContext


# policy_search 入参模型：查询文本、费用类型、生效日与 top_k（严格类型）
class PolicySearchInput(StrictModel):
    query: str = Field(min_length=1, max_length=1000)
    expense_type: ExpenseType
    effective_at: DateOnly
    top_k: int = Field(default=5, ge=1, le=20, strict=True)


def _prefer_authoritative_clauses(items: list[EvidenceItem], limit: int) -> list[EvidenceItem]:
    """Keep two high-priority clauses when similarity puts general policies first."""

    selected = items[:limit]
    if not selected or len(items) <= limit:
        return selected
    highest = max((item.priority or 0 for item in items), default=0)
    if highest <= 0:
        return selected
    needed = min(2, limit, sum(item.priority == highest for item in items))
    for candidate in items[limit:]:
        if sum(item.priority == highest for item in selected) >= needed:
            break
        if candidate.priority == highest:
            for index in range(len(selected) - 1, -1, -1):
                if selected[index].priority != highest:
                    selected[index] = candidate
                    break
    order = {item.evidence_id: index for index, item in enumerate(items)}
    return sorted(selected, key=lambda item: order[item.evidence_id])


# 面向 Agent 的制度检索工具：检索范围完全由服务端上下文提供
class PolicySearchTool:
    # 注入底层制度索引；P11 内部升级为混合检索但工具 Schema 不变
    def __init__(self, index: Any) -> None:
        self.index = index

    # 校验调用参数与服务端范围一致后执行检索，参数非法或跨范围一律返回错误观察
    def __call__(
        self,
        call: ToolCall,
        context: ToolExecutionContext,
        max_results: int,
        timeout_seconds: float,
    ) -> ToolObservation:
        if (
            call.query_type is not None
            or call.arguments
            or not set(call.filters) <= {"expense_type", "effective_at", "top_k"}
        ):
            return self._error(call, "INVALID_TOOL_INPUT")
        try:
            request = PolicySearchInput.model_validate({**call.filters, "query": call.query})
        except ValidationError:
            return self._error(call, "INVALID_TOOL_INPUT")
        if (
            request.expense_type != context.expense_type
            or request.effective_at != context.policy_effective_at
        ):
            return self._error(call, "AUTHORIZATION_SCOPE_MISMATCH")
        limit = min(request.top_k, max_results)
        items, usage = self.index.search(
            request.query,
            department_id=context.department_id,
            expense_type=request.expense_type.value,
            effective_at=request.effective_at,
            allowed_document_ids=context.allowed_document_ids,
            catalog_snapshot_id=(
                context.policy_source_snapshot_ids or context.policy_catalog_snapshot_id
            ),
            top_k=min(20, limit * 2),
            timeout_seconds=timeout_seconds,
        )
        items = _prefer_authoritative_clauses(items, limit)
        return ToolObservation(
            tool_name=ToolName.POLICY_SEARCH,
            query=call.query,
            items=items,
            source_ids=[item.evidence_id for item in items],
            latency_ms=0,
            is_degraded=bool(getattr(usage, "degraded_sources", ())),
            call_id=call.call_id,
        )

    # 构造统一格式的错误观察结果
    @staticmethod
    def _error(call: ToolCall, code: str) -> ToolObservation:
        return ToolObservation(
            tool_name=ToolName.POLICY_SEARCH,
            query=call.query,
            latency_ms=0,
            error=code,
            call_id=call.call_id,
        )
