"""Exercise P09 PostgreSQL checkpoints, interrupts, and idempotent recovery."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.contracts import (
    CoverageStatus,
    EvidenceAction,
    EvidenceReview,
    QuestionCoverage,
    RetrievalAction,
    RetrievalPlan,
    SubQuestion,
    ToolCall,
    ToolName,
    ToolObservation,
)
from app.database import PostgresStore
from app.graph.approval import PERSISTENCE_MAX_ATTEMPTS, ApprovalRuntime
from app.graph.state import validate_state
from app.graph.workflow import (
    approval_thread_id,
    open_persistent_approval_graph,
    resume_human_review,
    start_approval,
    workflow_consistency,
)
from app.models import (
    Applicant,
    Application,
    ApprovalStatus,
    EvidenceItem,
    EvidenceMetrics,
    EvidenceSource,
    HumanReviewSubmission,
)
from app.tools.registry import ToolExecutionContext, ToolRegistry, ToolSpec

_CREATED_REQUEST_IDS: list[str] = []
_EXPECTED_EVENT_TYPES = {
    "APPROVAL_CREATED",
    "HUMAN_REVIEW_REQUESTED",
    "HUMAN_REVIEWED",
    "APPROVAL_FINALIZED",
    "NOTIFICATION_REQUESTED",
    "CASE_WRITE_REQUESTED",
}


class InjectedFailure(RuntimeError):
    pass


class FailOnce:
    def __init__(self, point: str) -> None:
        self.point = point
        self.fired = False

    def __call__(self, point: str) -> None:
        if point == self.point and not self.fired:
            self.fired = True
            raise InjectedFailure(point)


class RetryDatabaseOnce:
    def __init__(self, point: str) -> None:
        self.point = point
        self.attempts = 0

    def __call__(self, point: str) -> None:
        if point == self.point:
            self.attempts += 1
            if self.attempts == 1:
                raise SQLAlchemyError("injected retryable persistence failure")


class FixedClient:
    def generate(self, schema, *, system_prompt, payload):
        del system_prompt
        if schema is RetrievalPlan:
            question_id = payload["required_questions"][0]["question_id"]
            return (
                RetrievalPlan(
                    action=RetrievalAction.RETRIEVE,
                    reason="查询适用交通制度",
                    sub_questions=[SubQuestion(question_id=question_id, text="核对交通制度")],
                    tool_calls=[
                        ToolCall(
                            tool_name=ToolName.POLICY_SEARCH,
                            query="交通费用制度",
                            purpose="查询交通制度",
                            sub_question_id=question_id,
                        )
                    ],
                ),
                1,
                1,
            )
        if schema is EvidenceReview:
            evidence_id = payload["provided_evidence_ids"][0]
            return (
                EvidenceReview(
                    recommended_action=EvidenceAction.REQUEST_DOCUMENTS,
                    reason="仍需申请人补充路线证明",
                    coverage=[
                        QuestionCoverage(
                            question_id=question["question_id"],
                            status=CoverageStatus.SUPPORTED,
                            evidence_ids=[evidence_id],
                        )
                        for question in payload["questions"]
                    ],
                    evidence_gaps=["路线证明"],
                ),
                1,
                1,
            )
        raise AssertionError(f"unexpected schema: {schema}")


def _policy_tool(
    call: ToolCall,
    context: ToolExecutionContext,
    max_results: int,
    timeout_seconds: float,
) -> ToolObservation:
    del context, max_results, timeout_seconds
    evidence = EvidenceItem(
        evidence_id="EVID-P09-POLICY",
        source_type=EvidenceSource.POLICY_DOCUMENT,
        document_id="POL-TRANSPORT-2026-V1",
        version="1.0",
        effective_from="2026-01-01",
        page=1,
        excerpt="公务交通应核对路线和业务目的。",
        catalog_snapshot_id="POLICY-CATALOG-P09",
        published_status="PUBLISHED",
    )
    return ToolObservation(
        tool_name=call.tool_name,
        query=call.query,
        items=[evidence],
        source_ids=[evidence.evidence_id],
        latency_ms=0,
        call_id=call.call_id,
    )


def _state(request_id: str, *, tool_path: bool) -> dict[str, object]:
    application = Application(
        expense_type="交通",
        currency="CNY",
        amount="30.00",
        occurred_on="2026-05-01",
        submitted_on="2026-05-02",
        description="P09 checkpoint smoke",
    )
    return {
        "request_id": request_id,
        "trace_id": f"TRACE-{request_id}",
        "status": ApprovalStatus.RUNNING,
        "applicant": Applicant(
            employee_id="EMP-P09",
            department_id="DEPT-FINANCE",
            display_name="合成审核样本",
        ),
        "application": application,
        "documents": [],
        "extracted_fields": [],
        "extraction_issues": [],
        "supervisor_decision": None,
        "supervisor_decisions": [],
        "supervisor_challenge_count": 0,
        "guardrail_decisions": [],
        "open_questions": [],
        "retrieval_plan": None,
        "evidence_review": None,
        "selected_tools": [],
        "query_variants": [],
        "tool_observations": [],
        "retrieved_candidates": [],
        "evidence": [],
        "evidence_metrics": EvidenceMetrics(),
        "agent_trajectory": [],
        "agent_step_count": 0,
        "agent_stop_reason": None,
        "rule_results": [],
        "risk_flags": [],
        "risk_level": None,
        "recommendation": None,
        "decision_reasons": [],
        "technical_retry_count": 0,
        "node_attempts": {},
        "retrieval_round_count": 0,
        "no_progress_rounds": 0,
        "query_rewrite_count": 0,
        "tokens_used": 0,
        "max_agent_steps": 8 if tool_path else 1,
        "max_retrieval_rounds": 3,
        "max_query_rewrites": 2,
        "token_budget": 2_000,
        "deadline_at": datetime.now(UTC) + timedelta(minutes=2),
        "pending_human_action": None,
        "final_decision": None,
        "human_review_idempotency_key": None,
    }


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        requester_role="APPLICANT",
        department_id="DEPT-FINANCE",
        allowed_department_ids=["DEPT-FINANCE"],
        allowed_document_ids=["POL-TRANSPORT-2026-V1"],
        allowed_tools=[ToolName.POLICY_SEARCH],
        policy_catalog_snapshot_id="POLICY-CATALOG-P09",
        structured_data_snapshot_id="STRUCTURED-DATA-P09",
        policy_effective_at="2026-05-01",
        structured_as_of="2026-05-02",
        expense_type="交通",
    )


def _registry() -> ToolRegistry:
    return ToolRegistry(
        [ToolSpec(ToolName.POLICY_SEARCH, _policy_tool, 1, 1)]
    )


def _runtime(store: PostgresStore, failure=None) -> ApprovalRuntime:
    return ApprovalRuntime(
        FixedClient(),
        _registry(),
        _context(),
        store,
        failure or (lambda _: None),
    )


def _submission(request_id: str) -> dict[str, object]:
    return {
        "action": "REJECT",
        "operator_id": "SPOOFED-OPERATOR",
        "reviewer_role": "APPLICANT",
        "reason": "人工核验后驳回",
        "idempotency_key": f"review:{request_id}",
    }


def _resume_after_restart(
    request_id: str,
    state: dict[str, object],
    creation_key: str,
) -> dict[str, object]:
    store = PostgresStore()
    try:
        with open_persistent_approval_graph() as graph:
            return start_approval(
                graph,
                state,
                _runtime(store),
                idempotency_key=creation_key,
            )
    finally:
        store.close()


def _agent_or_tool_failure(point: str) -> tuple[bool, dict[str, int]]:
    request_id = f"REQ-P09-{point.upper()}-{uuid4().hex[:8]}"
    _CREATED_REQUEST_IDS.append(request_id)
    tool_path = point == "after_tools"
    state = _state(request_id, tool_path=tool_path)
    creation_key = f"create:{request_id}"
    injector = FailOnce(point)
    store = PostgresStore()
    try:
        with open_persistent_approval_graph() as graph:
            try:
                start_approval(
                    graph,
                    state,
                    _runtime(store, injector),
                    idempotency_key=creation_key,
                )
            except InjectedFailure:
                pass
            else:
                return False, {}
    finally:
        store.close()
    paused = _resume_after_restart(request_id, state, creation_key)
    if not paused.get("__interrupt__"):
        return False, {}
    allowed_actions = paused["__interrupt__"][0].value["allowed_actions"]
    if tool_path and "APPROVE" in allowed_actions:
        return False, {}
    store = PostgresStore()
    try:
        persisted = store.approval_record(request_id)
        if persisted is None or persisted["status"] not in {
            ApprovalStatus.HUMAN_PENDING.value,
            ApprovalStatus.INSUFFICIENT_EVIDENCE.value,
        }:
            return False, {}
        with open_persistent_approval_graph() as graph:
            if workflow_consistency(graph, request_id, _runtime(store))[
                "consistency"
            ] != "CONSISTENT":
                return False, {}
            final = resume_human_review(
                graph,
                request_id,
                _runtime(store),
                _submission(request_id),
                operator_id="FIN-P09",
                reviewer_role="FINANCE_REVIEWER",
            )
            consistency = workflow_consistency(
                graph, request_id, _runtime(store)
            )["consistency"]
        validate_state(final)
        completed_by_server_actor = (
            final["status"] == ApprovalStatus.COMPLETED
            and final["final_decision"].operator_id == "FIN-P09"
        )
        return (
            completed_by_server_actor and consistency == "CONSISTENT",
            store.audit_event_counts(request_id),
        )
    finally:
        store.close()


def _finalize_failure(point: str) -> tuple[bool, dict[str, int]]:
    request_id = f"REQ-P09-{point.upper()}-{uuid4().hex[:8]}"
    _CREATED_REQUEST_IDS.append(request_id)
    state = _state(request_id, tool_path=False)
    creation_key = f"create:{request_id}"
    paused = _resume_after_restart(request_id, state, creation_key)
    if not paused.get("__interrupt__"):
        return False, {}
    injector = FailOnce(point)
    store = PostgresStore()
    transient_consistency: dict[str, object] = {}
    try:
        with open_persistent_approval_graph() as graph:
            try:
                resume_human_review(
                    graph,
                    request_id,
                    _runtime(store, injector),
                    _submission(request_id),
                    operator_id="FIN-P09",
                    reviewer_role="FINANCE_REVIEWER",
                )
            except InjectedFailure:
                pass
            else:
                return False, {}
            transient_consistency = workflow_consistency(
                graph, request_id, _runtime(store)
            )
    finally:
        store.close()
    expected_transient = "DIVERGED" if point == "before_finalize" else "CONSISTENT"
    if (
        transient_consistency.get("consistency") != expected_transient
        or transient_consistency.get("next_nodes") != ["finalize"]
    ):
        return False, {}
    store = PostgresStore()
    try:
        with open_persistent_approval_graph() as graph:
            final = resume_human_review(
                graph,
                request_id,
                _runtime(store),
                _submission(request_id),
                operator_id="FIN-P09",
                reviewer_role="FINANCE_REVIEWER",
            )
            duplicate = resume_human_review(
                graph,
                request_id,
                _runtime(store),
                _submission(request_id),
                operator_id="FIN-P09",
                reviewer_role="FINANCE_REVIEWER",
            )
            start_approval(
                graph,
                state,
                _runtime(store),
                idempotency_key=creation_key,
            )
            consistency = workflow_consistency(
                graph, request_id, _runtime(store)
            )["consistency"]
        validate_state(final)
        validate_state(duplicate)
        counts = store.audit_event_counts(request_id)
        exactly_once = set(counts) == _EXPECTED_EVENT_TYPES and all(
            counts.get(event_type) == 1 for event_type in _EXPECTED_EVENT_TYPES
        )
        server_actor_won = final["final_decision"].operator_id == "FIN-P09"
        return exactly_once and server_actor_won and consistency == "CONSISTENT", counts
    finally:
        store.close()


def _database_retry() -> tuple[bool, dict[str, int]]:
    request_id = f"REQ-P09-DATABASE-RETRY-{uuid4().hex[:8]}"
    _CREATED_REQUEST_IDS.append(request_id)
    state = _state(request_id, tool_path=False)
    creation_key = f"create:{request_id}"
    paused = _resume_after_restart(request_id, state, creation_key)
    if not paused.get("__interrupt__"):
        return False, {}
    injector = RetryDatabaseOnce("before_finalize")
    store = PostgresStore()
    try:
        with open_persistent_approval_graph() as graph:
            final = resume_human_review(
                graph,
                request_id,
                _runtime(store, injector),
                _submission(request_id),
                operator_id="FIN-P09",
                reviewer_role="FINANCE_REVIEWER",
            )
            consistency = workflow_consistency(
                graph, request_id, _runtime(store)
            )["consistency"]
        counts = store.audit_event_counts(request_id)
        return (
            injector.attempts == 2
            and final["status"] == ApprovalStatus.COMPLETED
            and consistency == "CONSISTENT",
            counts,
        )
    finally:
        store.close()


def _cleanup(request_ids: list[str]) -> None:
    for request_id in request_ids:
        with open_persistent_approval_graph() as graph:
            graph.checkpointer.delete_thread(approval_thread_id(request_id))
    store = PostgresStore()
    try:
        with store.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM audit_events WHERE request_id = ANY(:request_ids)"),
                {"request_ids": request_ids},
            )
            connection.execute(
                text("DELETE FROM approval_requests WHERE request_id = ANY(:request_ids)"),
                {"request_ids": request_ids},
            )
    finally:
        store.close()


def count_duplicate_side_effects(
    results: dict[str, dict[str, object]],
) -> int:
    return sum(
        max(0, count - 1)
        for item in results.values()
        for count in item["event_counts"].values()
    )


def run_recovery_smoke() -> dict[str, object]:
    _CREATED_REQUEST_IDS.clear()
    schema_rejected = False
    role_rejected = False
    try:
        HumanReviewSubmission(
            action="EDIT",
            operator_id="FIN-P09",
            reviewer_role="FINANCE_REVIEWER",
            reason="缺少修改后的结论",
            idempotency_key="invalid-edit",
        )
    except ValidationError:
        schema_rejected = True
    try:
        HumanReviewSubmission.model_validate(
            {
                "action": "APPROVE",
                "operator_id": "EMP-P09",
                "reviewer_role": "APPLICANT",
                "reason": "越权审批",
                "idempotency_key": "invalid-role",
            }
        )
    except ValidationError:
        role_rejected = True

    results: dict[str, dict[str, object]] = {}
    for point in ("before_agent", "after_agent", "after_tools"):
        passed, counts = _agent_or_tool_failure(point)
        results[point] = {"passed": passed, "event_counts": counts}
    for point in ("before_finalize", "after_finalize"):
        passed, counts = _finalize_failure(point)
        results[point] = {"passed": passed, "event_counts": counts}
    passed, counts = _database_retry()
    results["database_retry"] = {"passed": passed, "event_counts": counts}
    duplicate_side_effects = count_duplicate_side_effects(results)
    all_events_once = all(
        set(item["event_counts"]) == _EXPECTED_EVENT_TYPES
        and all(
            item["event_counts"].get(event_type) == 1
            for event_type in _EXPECTED_EVENT_TYPES
        )
        for item in results.values()
    )
    passed = schema_rejected and role_rejected and all_events_once and all(
        item["passed"] for item in results.values()
    ) and duplicate_side_effects == 0
    report = {
        "schema": "p09-recovery/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "postgres_checkpoint": True,
        "service_restart_simulated": True,
        "uses_fake_model": True,
        "uses_stub_tool": True,
        "scope": "INFRASTRUCTURE_RECOVERY_SMOKE",
        "persistence_retry_max_attempts": PERSISTENCE_MAX_ATTEMPTS,
        "authority_contract": {
            "execution": "LANGGRAPH_CHECKPOINT",
            "business": "APPROVAL_REQUESTS_AND_AUDIT_EVENTS",
            "mismatch_detection": True,
        },
        "schema_rejected": schema_rejected,
        "unauthorized_role_rejected": role_rejected,
        "failure_points": results,
        "duplicate_side_effects": duplicate_side_effects,
        "passed": passed,
    }
    _cleanup(_CREATED_REQUEST_IDS)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evals/reports/p09_recovery.json")
    args = parser.parse_args()
    report = run_recovery_smoke()
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
