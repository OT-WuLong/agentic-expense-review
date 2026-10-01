"""Post-finalization case memory and draft rules from human edits."""

from __future__ import annotations

import math
from typing import Any

from pydantic import Field

from app.agents.model import StructuredChatClient
from app.database import PostgresStore
from app.graph.state import ApprovalState
from app.models import EvidenceSource, HumanReviewAction, StrictModel
from app.retrieval.dense import QwenEmbeddingClient


class CandidateRuleDraft(StrictModel):
    target_rule_id: str | None = None
    summary: str | None = Field(default=None, max_length=300)
    parameters: dict[str, str | int] = Field(default_factory=dict)


_DRAFT_PROMPT = """你只从财务审核员对本次预审的 EDIT 修改中提炼可复用的数字规则建议。
只有人工理由明确提出制度额度或提交天数应改变，且 target_rule_id 在 allowed_rule_ids 中，
才输出 target_rule_id、简短 summary 和对应 parameters。否则输出 null 与空对象。
这只是 PENDING 候选；不得声称已发布，也不得发明新的制度或审批权限。"""


def case_payload(state: ApprovalState) -> dict[str, Any]:
    """Keep only business categories and citations; omit personal and free text."""

    application = state["application"]
    policy = [
        item
        for item in state.get("evidence", [])
        if item.source_type == EvidenceSource.POLICY_DOCUMENT
    ]
    rule_refs = [
        {
            "rule_id": item.rule_id,
            "version": item.rule_version,
            "outcome": item.outcome.value,
            "reason_code": item.reason_code,
        }
        for item in state.get("rule_results", [])
    ]
    evidence_refs = [
        {
            "document_id": item.document_id,
            "version": item.version,
            "page": item.page,
            "section": item.section,
        }
        for item in policy
    ]
    summary = " ".join(
        str(part)
        for part in [
            application.expense_type.value,
            *(item.section for item in policy[:3]),
            *(item["reason_code"] for item in rule_refs if item["reason_code"]),
        ]
        if part
    )
    return {
        "source_request_id": state["request_id"],
        "department_id": state["applicant"].department_id,
        "expense_type": application.expense_type.value,
        "occurred_on": application.occurred_on,
        "summary": summary or application.expense_type.value,
        "recommendation": state["recommendation"].value,
        "human_action": (
            state["final_decision"].action.value if state.get("final_decision") else None
        ),
        "rule_refs": rule_refs,
        "evidence_refs": evidence_refs,
    }


def rank_cases(
    rows: list[dict[str, Any]], query: str, vector: list[float] | None
) -> list[tuple[dict[str, Any], float]]:
    """Small single-company catalog: cosine when available, lexical fallback."""

    # ponytail: scan at most 100 department-scoped cases; use a vector index if history grows.
    query_chars = set(query)
    ranked = []
    for row in rows:
        stored = row.get("embedding")
        if vector and stored and len(vector) == len(stored):
            denominator = math.sqrt(sum(value * value for value in vector)) * math.sqrt(
                sum(value * value for value in stored)
            )
            score = sum(left * right for left, right in zip(vector, stored, strict=True)) / (
                denominator or 1
            )
        else:
            score = len(query_chars & set(row["summary"])) / (len(query_chars) or 1)
        ranked.append((row, score))
    return sorted(ranked, key=lambda item: (-item[1], item[0]["case_id"]))


def write_memory(
    state: ApprovalState,
    store: PostgresStore,
    *,
    embedder: QwenEmbeddingClient | None = None,
    model: StructuredChatClient | None = None,
) -> None:
    """Runs after graph finalization; callers log failures without changing approval status."""

    request_id = state["request_id"]
    if not store.case_write_requested(request_id):
        return
    if not store.case_exists(request_id):
        payload = case_payload(state)
        vector = None
        try:
            vectors, _ = (embedder or QwenEmbeddingClient()).embed(
                [payload["summary"]], text_type="document", timeout_seconds=20
            )
            vector = vectors[0]
        except Exception:  # noqa: BLE001, S110 - lexical case search remains available
            pass
        store.insert_case(**payload, embedding=vector)

    decision = state.get("final_decision")
    if not decision or decision.action != HumanReviewAction.EDIT:
        return
    if store.candidate_exists(request_id):
        return
    allowed = store.proposable_rule_ids(
        [item.rule_id for item in state.get("rule_results", [])]
    )
    if not allowed:
        return
    draft, _, _ = (model or StructuredChatClient(timeout_seconds=20)).generate(
        CandidateRuleDraft,
        system_prompt=_DRAFT_PROMPT,
        payload={
            "human_change": {
                "before": decision.previous_recommendation.value
                if decision.previous_recommendation
                else None,
                "after": decision.recommendation.value,
                "reason": decision.reason,
            },
            "allowed_rule_ids": allowed,
        },
    )
    if draft.target_rule_id in allowed and draft.parameters and draft.summary:
        store.insert_candidate(
            source_request_id=request_id,
            target_rule_id=draft.target_rule_id,
            department_id=state["applicant"].department_id,
            expense_type=state["application"].expense_type.value,
            summary=draft.summary,
            parameters=draft.parameters,
        )
