"""Read-only published rule catalog search; candidates are never visible here."""

from __future__ import annotations

from pydantic import ValidationError

from app.agents.contracts import ToolCall, ToolName, ToolObservation
from app.database import PostgresStore
from app.models import EvidenceItem, EvidenceSource
from app.tools.policy_search import PolicySearchInput
from app.tools.registry import ToolExecutionContext

_RULE_WORDS = {
    "amount_limit": "金额 额度 上限",
    "timeliness": "提交期限 逾期",
    "prohibition": "禁止报销",
    "document_consistency": "票据 材料 一致性",
    "structured_fact": "预算 权限 重复票据",
    "version_conflict": "制度版本 冲突",
}


class RuleSearchTool:
    def __init__(self, store: PostgresStore) -> None:
        self.store = store

    def __call__(
        self,
        call: ToolCall,
        context: ToolExecutionContext,
        max_results: int,
        timeout_seconds: float,
    ) -> ToolObservation:
        del timeout_seconds
        if call.query_type or call.arguments or not set(call.filters) <= {
            "expense_type", "effective_at", "top_k"
        }:
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
        rules = self.store.policy_rules(
            expense_type=request.expense_type,
            effective_at=request.effective_at,
            document_ids=set(context.allowed_document_ids),
        )
        query_chars = set(request.query)
        ranked = sorted(
            rules,
            key=lambda rule: (
                -len(
                    query_chars
                    & set(
                        f"{rule.rule_id} {rule.rule_type} "
                        f"{_RULE_WORDS.get(rule.rule_type, '')} {rule.source_document_id}"
                    )
                ),
                rule.rule_id,
            ),
        )[: min(request.top_k, max_results)]
        items = [
            EvidenceItem(
                evidence_id=(
                    f"rule:{rule.rule_id}:{rule.rule_version}:"
                    f"{rule.source_document_id or 'GLOBAL'}"
                ),
                source_type=EvidenceSource.RULE_CATALOG,
                rule_id=rule.rule_id,
                version=rule.rule_version,
                document_id=rule.source_document_id,
                effective_from=rule.effective_from,
                effective_to=rule.effective_to,
                excerpt=(
                    f"已发布规则 {rule.rule_id}，类型 {rule.rule_type}，"
                    f"参数 {rule.parameters}；仅作规则目录说明，以制度原文和规则引擎为准。"
                ),
                published_status="PUBLISHED",
            )
            for rule in ranked
        ]
        return ToolObservation(
            tool_name=ToolName.RULE_SEARCH,
            query=call.query,
            items=items,
            source_ids=[item.evidence_id for item in items],
            latency_ms=0,
            call_id=call.call_id,
        )

    @staticmethod
    def _error(call: ToolCall, code: str) -> ToolObservation:
        return ToolObservation(
            tool_name=ToolName.RULE_SEARCH,
            query=call.query,
            latency_ms=0,
            error=code,
            call_id=call.call_id,
        )
