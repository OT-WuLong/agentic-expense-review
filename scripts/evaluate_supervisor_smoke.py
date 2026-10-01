"""Deterministic smoke evaluation for Supervisor and Guardrail control paths."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.contracts import (
    AgentAction,
    AgentRole,
    AgentStep,
    CoverageStatus,
    EvidenceAction,
    EvidenceReview,
    QuestionCoverage,
    RetrievalAction,
    RetrievalPlan,
    SubQuestion,
    SupervisorAction,
    SupervisorDecision,
    SupervisorDecisionRecord,
    ToolCall,
    ToolName,
    ToolObservation,
)
from app.agents.retrieval import plan_retrieval
from app.agents.supervisor import MAX_SUPERVISOR_CHALLENGES, decide_supervisor
from app.graph.approval import (
    _post_review_guardrail_node,
    _pre_tool_guardrail_node,
    _retrieval_node,
    _route_pre_tool_guardrail,
)
from app.models import Application, ApprovalStatus


class FixedClient:
    def __init__(self, output: object) -> None:
        self.output = output

    def generate(self, schema, *, system_prompt, payload):
        del schema, system_prompt, payload
        return self.output, 1, 1


def _state() -> dict[str, object]:
    base_question = SubQuestion(question_id="Q0", text="核对原始适用制度")
    challenged_question = SubQuestion(
        question_id="Q1", text="核对制度权威与替代关系"
    )
    call = ToolCall(
        tool_name=ToolName.POLICY_SEARCH,
        query="餐饮限额",
        purpose="检索限额",
        sub_question_id="Q1",
        call_id="R01-C01",
    )
    return {
        "application": Application(
            expense_type="餐饮",
            currency="CNY",
            amount="135.00",
            occurred_on="2026-06-15",
            submitted_on="2026-06-18",
            attendee_count=1,
            description="Supervisor challenge smoke fixture",
        ),
        "retrieval_goal": "查询餐饮限额",
        "evidence_review": EvidenceReview(
            recommended_action=EvidenceAction.SUFFICIENT,
            reason="故意提前判断充分",
            coverage=[
                QuestionCoverage(
                    question_id="Q0",
                    status=CoverageStatus.SUPPORTED,
                    evidence_ids=["E-SMOKE-1"],
                ),
                QuestionCoverage(
                    question_id="Q1",
                    status=CoverageStatus.SUPPORTED,
                    evidence_ids=["E-SMOKE-1"],
                )
            ],
        ),
        "open_questions": [base_question, challenged_question],
        "selected_tools": [call],
        "tool_observations": [
            ToolObservation(
                tool_name=ToolName.POLICY_SEARCH,
                query="餐饮限额",
                latency_ms=1,
                call_id="R01-C01",
            )
        ],
        "agent_trajectory": [
            AgentStep(
                step_index=1,
                agent=AgentRole.RETRIEVAL,
                action=AgentAction.RETRIEVE,
                thought_summary="首次查询",
                tool_calls=[call],
                latency_ms=1,
                tokens_used=1,
            )
        ],
        "evidence": [],
        "retrieved_candidates": [],
        "supervisor_challenge_count": 0,
        "supervisor_decisions": [],
        "guardrail_decisions": [],
        "agent_step_count": 2,
        "technical_retry_count": 0,
        "max_agent_steps": 8,
        "retrieval_round_count": 1,
        "max_retrieval_rounds": 3,
        "query_rewrite_count": 0,
        "max_query_rewrites": 2,
        "tokens_used": 2,
        "token_budget": 20_000,
        "no_progress_rounds": 0,
        "risk_flags": [],
        "deadline_at": datetime.now(UTC) + timedelta(minutes=2),
    }


def main() -> None:
    state = _state()
    challenge = SupervisorDecision(
        action=SupervisorAction.CHALLENGE_AND_RETRIEVE,
        reason="尚未核对制度权威与替代关系",
        retrieval_goal="定向核对同期制度的权威、替代和过渡条款",
        referenced_call_ids=["R01-C01"],
        challenged_question_ids=["Q1"],
    )
    decision, _, _, used_model, _ = decide_supervisor(FixedClient(challenge), state)
    state["supervisor_decision"] = decision

    valid_plan = RetrievalPlan(
        action=RetrievalAction.RETRIEVE_MORE,
        reason="执行 Supervisor 挑战",
        sub_questions=[SubQuestion(question_id="Q1", text="核对制度权威与替代关系")],
        tool_calls=[
            ToolCall(
                tool_name=ToolName.POLICY_SEARCH,
                query="制度替代关系 过渡条款 正式发布",
                purpose="核对权威与替代关系",
                sub_question_id="Q1",
            )
        ],
    )
    plan, _, _ = plan_retrieval(
        FixedClient(valid_plan), state, [ToolName.POLICY_SEARCH]
    )
    retrieval_update = _retrieval_node(
        state,
        SimpleNamespace(
            context=SimpleNamespace(
                model=FixedClient(valid_plan),
                tool_context=SimpleNamespace(allowed_tools=[ToolName.POLICY_SEARCH]),
            )
        ),
    )

    unrelated_plan_rejected = False
    try:
        plan_retrieval(
            FixedClient(
                RetrievalPlan(
                    action=RetrievalAction.RETRIEVE_MORE,
                    reason="错误地改查其他问题",
                    sub_questions=[SubQuestion(question_id="Q2", text="无关问题")],
                    tool_calls=[
                        ToolCall(
                            tool_name=ToolName.POLICY_SEARCH,
                            query="无关查询",
                            purpose="无关目的",
                            sub_question_id="Q2",
                        )
                    ],
                )
            ),
            state,
            [ToolName.POLICY_SEARCH],
        )
    except ValueError:
        unrelated_plan_rejected = True

    veto_state = {
        **state,
        **retrieval_update,
        "query_rewrite_count": 1,
        "max_query_rewrites": 0,
        "supervisor_decisions": [
            SupervisorDecisionRecord(
                sequence=1,
                decision_source="LLM",
                trigger="REVIEW_SUFFICIENT",
                action=SupervisorAction.CHALLENGE_AND_RETRIEVE,
                reason=challenge.reason,
                previous_goal="查询餐饮限额",
                new_goal=challenge.retrieval_goal,
                used_model=True,
                referenced_call_ids=["R01-C01"],
                challenged_question_ids=["Q1"],
                latency_ms=1,
                tokens_used=1,
            )
        ],
        "guardrail_decisions": [],
    }
    pre_tool_update = _pre_tool_guardrail_node(veto_state, None)
    pre_tool_records = pre_tool_update.get("guardrail_decisions", [])

    soft_stop_state = {
        **state,
        "no_progress_rounds": 2,
        "guardrail_decisions": pre_tool_records,
        "evidence_review": EvidenceReview(
            recommended_action=EvidenceAction.RETRIEVE_MORE,
            reason="仍缺少权威依据",
            coverage=[
                QuestionCoverage(question_id="Q1", status=CoverageStatus.MISSING)
            ],
            evidence_gaps=["缺少权威依据"],
        ),
    }
    soft_stop_update = _post_review_guardrail_node(soft_stop_state)
    soft_stop_decision, *_ = decide_supervisor(
        FixedClient(challenge), soft_stop_state
    )

    post_review_state = {
        **soft_stop_state,
        "no_progress_rounds": 0,
        "tokens_used": 20_001,
    }
    post_review_update = _post_review_guardrail_node(post_review_state)
    post_review_records = post_review_update.get("guardrail_decisions", [])

    sufficient_state = {
        **state,
        "supervisor_challenge_count": 1,
    }
    sufficient_decision, *_ = decide_supervisor(
        FixedClient(challenge), sufficient_state
    )
    model_validate_decision, _, _, validate_used_model, _ = decide_supervisor(
        FixedClient(
            SupervisorDecision(
                action=SupervisorAction.VALIDATE,
                reason="接受 Reviewer 结论",
                stop_reason="MODEL_DEFINED_STOP",
                referenced_call_ids=["R01-C01"],
            )
        ),
        state,
    )
    request_documents_state = {
        **state,
        "evidence_review": EvidenceReview(
            recommended_action=EvidenceAction.REQUEST_DOCUMENTS,
            reason="缺少消费明细",
            evidence_gaps=["消费明细"],
        ),
    }
    request_documents_decision, *_ = decide_supervisor(
        FixedClient(challenge), request_documents_state
    )
    conflict_state = {
        **state,
        "evidence_review": EvidenceReview(
            recommended_action=EvidenceAction.ESCALATE,
            reason="制度冲突",
            conflict_evidence_ids=["E1", "E2"],
        ),
    }
    conflict_decision, *_ = decide_supervisor(
        FixedClient(challenge), conflict_state
    )

    open_question_ids = [
        item.question_id for item in retrieval_update["open_questions"]
    ]
    pre_tool_recorded = bool(
        pre_tool_records
        and pre_tool_records[0].phase == "PRE_TOOL"
        and getattr(pre_tool_records[0], "subject_role", None) == AgentRole.RETRIEVAL
        and pre_tool_records[0].related_supervisor_sequence == 1
    )
    post_review_recorded = bool(
        post_review_records
        and post_review_records[0].phase == "POST_REVIEW"
        and post_review_records[0].sequence == 2
        and getattr(post_review_records[0], "subject_role", None)
        == AgentRole.EVIDENCE_REVIEWER
    )
    guardrail_preserves_review = bool(
        "evidence_review" not in pre_tool_update
        and "evidence_review" not in post_review_update
    )
    hard_violations_are_system_errors = bool(
        pre_tool_update.get("status") == ApprovalStatus.SYSTEM_ERROR
        and post_review_update.get("status") == ApprovalStatus.SYSTEM_ERROR
        and _route_pre_tool_guardrail({**veto_state, **pre_tool_update}) == "end"
    )
    soft_stop_owned_by_supervisor = bool(
        soft_stop_update == {}
        and soft_stop_decision.action == SupervisorAction.ESCALATE
        and soft_stop_decision.stop_reason == "NO_NEW_EVIDENCE"
    )
    canonical_stop_reasons = bool(
        sufficient_decision.stop_reason == "ALL_NECESSARY_EVIDENCE_COVERED"
        and validate_used_model
        and model_validate_decision.stop_reason
        == "ALL_NECESSARY_EVIDENCE_COVERED"
        and request_documents_decision.stop_reason == "REQUEST_DOCUMENTS"
        and conflict_decision.stop_reason == "UNRESOLVED_POLICY_VERSION_CONFLICT"
    )

    golden_dataset = json.loads(
        (ROOT / "data/fixtures/golden_cases.json").read_text(encoding="utf-8")
    )
    trajectory_policy = golden_dataset["trajectory_policy"]
    contract_matches_runtime = bool(
        trajectory_policy["max_supervisor_challenges"]
        == MAX_SUPERVISOR_CHALLENGES
        and trajectory_policy["challenge_requires_all_target_questions_and_calls"]
        and trajectory_policy["guardrail_veto_must_reference_supervisor_decision"]
    )
    passed = (
        used_model
        and decision.action == SupervisorAction.CHALLENGE_AND_RETRIEVE
        and {item.question_id for item in plan.sub_questions} == {"Q1"}
        and {item.sub_question_id for item in plan.tool_calls} == {"Q1"}
        and set(open_question_ids) == {"Q0", "Q1"}
        and retrieval_update["query_rewrite_count"] == 1
        and unrelated_plan_rejected
        and pre_tool_recorded
        and post_review_recorded
        and guardrail_preserves_review
        and hard_violations_are_system_errors
        and soft_stop_owned_by_supervisor
        and canonical_stop_reasons
        and contract_matches_runtime
    )
    report = {
        "schema": "supervisor-guardrail-smoke/v3",
        "golden_dataset_version": golden_dataset["dataset_version"],
        "generated_at": datetime.now(UTC).isoformat(),
        "uses_fake_model": True,
        "passed": passed,
        "challenge_action": decision.action.value,
        "challenged_question_ids": decision.challenged_question_ids,
        "planned_question_ids": [item.question_id for item in plan.sub_questions],
        "planned_call_question_ids": [item.sub_question_id for item in plan.tool_calls],
        "open_question_ids_after_challenge": open_question_ids,
        "query_rewrite_count": retrieval_update["query_rewrite_count"],
        "unrelated_plan_rejected": unrelated_plan_rejected,
        "pre_tool_veto_recorded": pre_tool_recorded,
        "post_review_stop_recorded": post_review_recorded,
        "guardrail_preserves_review": guardrail_preserves_review,
        "hard_violations_are_system_errors": hard_violations_are_system_errors,
        "soft_stop_owned_by_supervisor": soft_stop_owned_by_supervisor,
        "canonical_stop_reasons": canonical_stop_reasons,
        "model_validate_stop_reason": model_validate_decision.stop_reason,
        "guardrail_decisions": [
            item.model_dump(mode="json")
            for item in [*pre_tool_records, *post_review_records]
        ],
        "contract_matches_runtime": contract_matches_runtime,
    }
    output = ROOT / "evals/reports/supervisor_challenge_smoke.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
