"""Read-only search over anonymized, completed approval cases."""

from __future__ import annotations

from pydantic import ValidationError

from app.agents.contracts import ToolCall, ToolName, ToolObservation
from app.database import PostgresStore
from app.memory import rank_cases
from app.models import EvidenceItem, EvidenceSource
from app.retrieval.dense import QwenEmbeddingClient
from app.tools.policy_search import PolicySearchInput
from app.tools.registry import ToolExecutionContext


class CaseSearchTool:
    def __init__(self, store: PostgresStore, embedder: QwenEmbeddingClient | None) -> None:
        self.store = store
        self.embedder = embedder

    def __call__(
        self,
        call: ToolCall,
        context: ToolExecutionContext,
        max_results: int,
        timeout_seconds: float,
    ) -> ToolObservation:
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
        rows = self.store.case_candidates(
            department_id=context.department_id,
            expense_type=request.expense_type.value,
            effective_at=request.effective_at,
        )
        if not rows:
            return ToolObservation(
                tool_name=ToolName.CASE_SEARCH,
                query=call.query,
                latency_ms=0,
                call_id=call.call_id,
            )
        vector = None
        degraded = False
        try:
            vectors, _ = (self.embedder or QwenEmbeddingClient()).embed(
                [request.query], text_type="query", timeout_seconds=min(timeout_seconds, 20)
            )
            vector = vectors[0]
        except Exception:  # noqa: BLE001 - case similarity has a lexical fallback
            degraded = True
        ranked = rank_cases(rows, request.query, vector)[: min(request.top_k, max_results)]
        items = [
            EvidenceItem(
                evidence_id=f"case:{row['case_id']}",
                source_type=EvidenceSource.CASE_MEMORY,
                case_id=row["case_id"],
                excerpt="\n".join(
                    [
                        row["summary"],
                        "规则："
                        + "、".join(
                            f"{item['rule_id']}@{item['version']}:{item['outcome']}"
                            for item in row["rule_refs"][:5]
                        ),
                        "制度："
                        + "、".join(
                            f"{item['document_id']}@{item['version']} p{item['page']}"
                            for item in row["evidence_refs"][:3]
                        ),
                    ]
                ),
                value=row["recommendation"],
                score=score,
            )
            for row, score in ranked
        ]
        return ToolObservation(
            tool_name=ToolName.CASE_SEARCH,
            query=call.query,
            items=items,
            source_ids=[item.evidence_id for item in items],
            latency_ms=0,
            is_degraded=degraded,
            call_id=call.call_id,
        )

    @staticmethod
    def _error(call: ToolCall, code: str) -> ToolObservation:
        return ToolObservation(
            tool_name=ToolName.CASE_SEARCH,
            query=call.query,
            latency_ms=0,
            error=code,
            call_id=call.call_id,
        )
