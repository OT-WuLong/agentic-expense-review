"""One bounded PostgreSQL smoke for case search and rule publication/rollback."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.contracts import ToolCall, ToolName
from app.database import IdempotencyConflictError, PostgresStore
from app.graph.state import ApprovalState
from app.memory import CandidateRuleDraft, write_memory
from app.models import (
    ApprovalInput,
    ApprovalStatus,
    ExpenseType,
    FinalDecision,
    HumanReviewAction,
    Recommendation,
    RuleResult,
)
from app.retrieval.dense import EmbeddingUsage
from app.tools.case_search import CaseSearchTool
from app.tools.registry import ToolExecutionContext
from app.tools.rule_search import RuleSearchTool


class FakeEmbedder:
    def embed(self, texts: list[str], *, text_type: str, timeout_seconds: float):
        return [[1.0] + [0.0] * 1023 for _ in texts], EmbeddingUsage()


class FakeDraftModel:
    def generate(self, *args, **kwargs):
        return (
            CandidateRuleDraft(
                target_rule_id="RULE-MEAL-LIMIT-USING-NEW-POLICY",
                summary="人工修改建议复核餐饮人均限额",
                parameters={"per_person": "150.00"},
            ),
            0,
            0,
        )


def main() -> None:
    golden = json.loads((ROOT / "data/fixtures/golden_cases.json").read_text(encoding="utf-8"))
    source = next(case for case in golden["cases"] if case["case_id"].startswith("GC-C-"))
    approval = ApprovalInput.model_validate(source["input"])
    request_id = f"REQ-P12-SMOKE-{uuid4().hex[:12]}"
    today = datetime.now(UTC).date()
    future = today + timedelta(days=2)
    decision = FinalDecision(
        action=HumanReviewAction.EDIT,
        operator_id="FIN-P12-SMOKE",
        recommendation=Recommendation.PASS_RECOMMENDED,
        previous_recommendation=Recommendation.HUMAN_REVIEW,
        reason="人工核对新版餐饮制度的人均150元标准，纠正此前误用的120元旧标准",
    )
    state = cast(
        ApprovalState,
        {
            "request_id": request_id,
            "applicant": approval.applicant,
            "application": approval.application,
            "recommendation": Recommendation.PASS_RECOMMENDED,
            "final_decision": decision,
            "rule_results": [
                RuleResult.model_validate(item) for item in source["expected_rule_results"]
            ],
            "evidence": [],
        },
    )
    store = PostgresStore()
    candidate_id = None
    published = False
    report: dict[str, object] = {"schema": "p12-memory-smoke/v1", "status": "FAILED"}
    try:
        store.create_approval(
            request_id=request_id,
            thread_id=f"approval:{request_id}",
            idempotency_key=f"p12-smoke:{request_id}",
            applicant=approval.applicant,
            application=approval.application,
            documents=[],
        )
        store.finalize_approval(
            request_id=request_id,
            status=ApprovalStatus.COMPLETED,
            recommendation=Recommendation.PASS_RECOMMENDED,
            risk_level=None,
            final_decision=decision,
            human_idempotency_key=request_id,
        )
        write_memory(state, store, embedder=FakeEmbedder(), model=FakeDraftModel())
        write_memory(state, store, embedder=FakeEmbedder(), model=FakeDraftModel())
        with store.engine.connect() as connection:
            candidate_id = connection.execute(
                text("SELECT candidate_id FROM candidate_rules WHERE source_request_id = :id"),
                {"id": request_id},
            ).scalar_one()
        assert store.case_exists(request_id)
        assert store.audit_event_counts(request_id)["CASE_WRITTEN"] == 1

        def context(department_id: str) -> ToolExecutionContext:
            return ToolExecutionContext(
                requester_role="APPLICANT",
                department_id=department_id,
                allowed_department_ids=[department_id],
                allowed_document_ids=["POL-DINING-2026-V2"],
                allowed_tools=[ToolName.CASE_SEARCH, ToolName.RULE_SEARCH],
                policy_catalog_snapshot_id="P12-SMOKE",
                structured_data_snapshot_id="P12-SMOKE",
                policy_effective_at=future,
                structured_as_of=future,
                expense_type=ExpenseType("餐饮"),
            )

        call = ToolCall(
            tool_name=ToolName.CASE_SEARCH,
            query="餐饮 人均限额",
            purpose="参考类似人工处理",
            filters={"expense_type": "餐饮", "effective_at": future.isoformat()},
        )
        search = CaseSearchTool(store, FakeEmbedder())
        same = search(call, context(approval.applicant.department_id), 3, 5)
        other = search(call, context("DEPT-OTHER"), 3, 5)
        assert same.items and other.items == []
        assert all(approval.applicant.employee_id not in item.excerpt for item in same.items)

        try:
            store.publish_rule(candidate_id, reviewer_id="FIN-P12-SMOKE")
        except IdempotencyConflictError:
            pass
        else:
            raise AssertionError("unreviewed candidate was published")

        store.approve_candidate(
            candidate_id,
            reviewer_id="FIN-P12-SMOKE",
            source_document_id="POL-DINING-2026-V2",
            effective_from=future,
            parameters={"per_person": "150.00"},
            replay_cases=[
                {"amount": "100.00", "attendees": 1, "expected": "PASS"},
                {"amount": "200.00", "attendees": 1, "expected": "FAIL"},
            ],
        )
        rule_id = store.publish_rule(candidate_id, reviewer_id="FIN-P12-SMOKE")
        published = True
        rule_call = ToolCall(
            tool_name=ToolName.RULE_SEARCH,
            query="RULE-MEAL-LIMIT-USING-NEW-POLICY 餐饮 限额",
            purpose="查看已发布规则",
            filters={"expense_type": "餐饮", "effective_at": future.isoformat(), "top_k": 20},
        )
        listed = RuleSearchTool(store)(rule_call, context(approval.applicant.department_id), 20, 2)
        assert any(item.rule_id == rule_id and item.version == f"P12-{candidate_id}" for item in listed.items)
        store.rollback_rule(candidate_id, reviewer_id="FIN-P12-SMOKE")
        published = False
        restored = store.policy_rules(
            expense_type=ExpenseType("餐饮"),
            effective_at=future,
            document_ids={"POL-DINING-2026-V2"},
        )
        assert any(item.rule_id == rule_id and item.rule_version == "1.0" for item in restored)
        report = {
            "schema": "p12-memory-smoke/v1",
            "status": "PASS",
            "case_write_count": 1,
            "same_department_results": len(same.items),
            "other_department_results": len(other.items),
            "published_rule_version": f"P12-{candidate_id}",
            "rollback_restored_version": "1.0",
            "unreviewed_publish_blocked": True,
            "model": "FakeDraftModel; tests workflow wiring only",
            "case_memory_ablation": {
                "status": "NOT_MEASURED",
                "reason": "No independent historical-case evaluation split is frozen yet.",
            },
        }
    finally:
        if published and candidate_id is not None:
            store.rollback_rule(candidate_id, reviewer_id="FIN-P12-SMOKE")
        with store.engine.begin() as connection:
            if candidate_id is not None:
                connection.execute(
                    text("DELETE FROM policy_rules WHERE id = :id AND enabled = false"),
                    {"id": f"candidate-{candidate_id}"},
                )
            connection.execute(
                text("DELETE FROM audit_events WHERE request_id = :id"),
                {"id": request_id},
            )
            connection.execute(
                text("DELETE FROM approval_requests WHERE request_id = :id"),
                {"id": request_id},
            )
        store.close()
    output = ROOT / "evals/reports/p12_memory_smoke.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
