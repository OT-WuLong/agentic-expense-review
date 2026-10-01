"""Bounded approval graph with Agentic Retrieval and human interruption."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy, interrupt
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.agents.contracts import (
    AgentAction,
    AgentRole,
    AgentStep,
    EvidenceAction,
    EvidenceReview,
    GuardrailDecision,
    SupervisorAction,
    SupervisorDecisionRecord,
    ToolObservation,
)
from app.agents.evidence_reviewer import review_evidence, valid_evidence
from app.agents.model import StructuredChatClient
from app.agents.retrieval import plan_retrieval
from app.agents.review_scope import required_questions
from app.agents.supervisor import decide_reviewer_direct, decide_supervisor
from app.database import PostgresStore
from app.graph.budget import hard_limit_violation_reason
from app.graph.routing import route_risk
from app.graph.state import ApprovalState
from app.models import (
    ApprovalStatus,
    DecisionReason,
    FinalDecision,
    HumanAction,
    HumanActionType,
    HumanReviewAction,
    HumanReviewPayload,
    HumanReviewSubmission,
    Recommendation,
)
from app.rules import evaluate_rules
from app.tools.registry import ToolExecutionContext, ToolRegistry, tool_call_hash


def _ignore_failure(_: str) -> None:
    pass


def _inject_failure(runtime: Runtime[ApprovalRuntime], point: str) -> None:
    getattr(runtime.context, "failure_injector", _ignore_failure)(point)


PERSISTENCE_MAX_ATTEMPTS = 3
LOGGER = logging.getLogger(__name__)
_PERSISTENCE_RETRY_POLICY = RetryPolicy(
    initial_interval=0.2,
    backoff_factor=2,
    max_interval=1,
    max_attempts=PERSISTENCE_MAX_ATTEMPTS,
    jitter=False,
    retry_on=SQLAlchemyError,
)


@dataclass(frozen=True, slots=True)
class ApprovalRuntime:
    model: StructuredChatClient
    registry: ToolRegistry
    tool_context: ToolExecutionContext
    database: PostgresStore
    failure_injector: Callable[[str], None] = _ignore_failure
    persist_workflow: bool = True
    supervisor_mode: Literal["llm", "reviewer_direct"] = "llm"


def _failure(state: ApprovalState, node: str) -> dict[str, object]:
    return {
        "status": ApprovalStatus.SYSTEM_ERROR,
        "technical_retry_count": state["technical_retry_count"] + 1,
        "agent_stop_reason": f"{node.upper()}_FAILED",
    }


def _supervisor_node(state: ApprovalState, runtime: Runtime[ApprovalRuntime]) -> dict[str, object]:
    _inject_failure(runtime, "before_agent")
    try:
        decision, tokens, latency_ms, used_model, repair_count = (
            decide_reviewer_direct(state)
            if runtime.context.supervisor_mode == "reviewer_direct"
            else decide_supervisor(runtime.context.model, state)
        )
    except Exception as exc:  # noqa: BLE001 - model boundary becomes a controlled workflow state
        LOGGER.warning("Supervisor failed: %s", type(exc).__name__)
        return _failure(state, "supervisor")
    _inject_failure(runtime, "after_agent")

    review = state.get("evidence_review")
    trigger = "INITIAL" if review is None else f"REVIEW_{review.recommended_action.value}"
    update: dict[str, object] = {
        "supervisor_decision": decision,
        "supervisor_decisions": [
            SupervisorDecisionRecord(
                sequence=len(state.get("supervisor_decisions", [])) + 1,
                decision_source="LLM" if used_model else "DETERMINISTIC",
                trigger=trigger,
                action=decision.action,
                reason=decision.reason,
                previous_goal=state.get("retrieval_goal"),
                new_goal=decision.retrieval_goal,
                stop_reason=decision.stop_reason,
                used_model=used_model,
                referenced_call_ids=decision.referenced_call_ids,
                challenged_question_ids=decision.challenged_question_ids,
                latency_ms=latency_ms,
                tokens_used=tokens,
            )
        ],
    }
    if used_model:
        update.update(
            agent_step_count=state["agent_step_count"] + 1,
            technical_retry_count=state["technical_retry_count"] + repair_count,
            tokens_used=state["tokens_used"] + tokens,
            agent_trajectory=[
                AgentStep(
                    step_index=state["agent_step_count"] + 1,
                    agent=AgentRole.SUPERVISOR,
                    action=AgentAction(decision.action.value),
                    thought_summary=decision.reason,
                    latency_ms=latency_ms,
                    tokens_used=tokens,
                    stop_reason=decision.stop_reason,
                )
            ],
        )
    if decision.retrieval_goal:
        update["retrieval_goal"] = decision.retrieval_goal
    if decision.action in {
        SupervisorAction.RETRIEVE,
        SupervisorAction.CHALLENGE_AND_RETRIEVE,
    }:
        update["agent_stop_reason"] = None
    elif decision.stop_reason:
        update["agent_stop_reason"] = decision.stop_reason
    if decision.action == SupervisorAction.CHALLENGE_AND_RETRIEVE:
        update["supervisor_challenge_count"] = state["supervisor_challenge_count"] + 1
    if decision.action == SupervisorAction.REQUEST_DOCUMENTS:
        update.update(
            status=ApprovalStatus.INSUFFICIENT_EVIDENCE,
            pending_human_action=HumanAction(
                action=HumanActionType.REQUEST_DOCUMENTS,
                reason=decision.reason,
                missing_items=(
                    state.get("evidence_review")
                    or EvidenceReview(
                        recommended_action=EvidenceAction.REQUEST_DOCUMENTS,
                        reason=decision.reason,
                    )
                ).evidence_gaps,
            ),
        )
    elif decision.action == SupervisorAction.ESCALATE:
        update.update(
            status=ApprovalStatus.HUMAN_PENDING,
            pending_human_action=HumanAction(
                action=HumanActionType.REVIEW,
                reason=decision.reason,
            ),
        )
    return update


def _retrieval_node(state: ApprovalState, runtime: Runtime[ApprovalRuntime]) -> dict[str, object]:
    _inject_failure(runtime, "before_agent")
    scope = runtime.context.tool_context
    review_questions = required_questions(state, scope)
    try:
        repair_count = 0
        repair_hint = ""
        for attempt in range(2):
            try:
                plan, tokens, latency_ms = plan_retrieval(
                    runtime.context.model,
                    state,
                    runtime.context.tool_context.allowed_tools,
                    allowed_structured_query_types=runtime.context.tool_context.allowed_structured_query_types,
                    required_questions=review_questions,
                    applicable_policy_ids=scope.allowed_document_ids,
                    applicable_rule_ids=scope.applicable_rule_ids,
                    rule_scope_complete=scope.rule_scope_complete,
                    repair_feedback=(
                        "上一份计划未通过 RetrievalPlan 或工具参数校验；只使用 allowed_tools，"
                        "每个调用关联现有子问题，filters/arguments 均填空对象，"
                        "并补齐 structured_lookup 的 query_type。"
                        f"本轮允许工具：{', '.join(item.value for item in scope.allowed_tools)}。"
                        + repair_hint
                        + (
                            "Q-EXCEPTION 必须有单独的 policy_search，查询会议酒店例外材料与批准角色。"
                            if any(item.question_id == "Q-EXCEPTION" for item in review_questions)
                            else ""
                        )
                        if attempt
                        else None
                    ),
                )
                break
            except ValueError as exc:
                if attempt == 1:
                    raise
                repair_count = 1
                if "unauthorized tool" in str(exc):
                    repair_hint = "上次含未授权工具，请删除该调用后重新规划。"
                elif "unavailable query" in str(exc):
                    repair_hint = "上次结构化查询类型不在允许列表，请改用允许的查询类型。"
    except Exception as exc:  # noqa: BLE001 - model/schema failures must not leak as business results
        errors = (
            [(item["loc"], item["type"]) for item in exc.errors()]
            if isinstance(exc, ValidationError)
            else f"{type(exc).__name__}: {str(exc)[:180]}"
        )
        LOGGER.warning("Retrieval Agent failed: %s", errors)
        return _failure(state, "retrieval")
    _inject_failure(runtime, "after_agent")

    return {
        "retrieval_plan": plan,
        "open_questions": review_questions,
        "selected_tools": plan.tool_calls,
        "query_variants": [item.query for item in plan.tool_calls],
        "retrieval_round_count": state["retrieval_round_count"] + (1 if plan.tool_calls else 0),
        "query_rewrite_count": state["query_rewrite_count"] + (1 if plan.is_query_rewrite else 0),
        "agent_step_count": state["agent_step_count"] + 1,
        "technical_retry_count": state["technical_retry_count"] + repair_count,
        "tokens_used": state["tokens_used"] + tokens,
        "agent_trajectory": [
            AgentStep(
                step_index=state["agent_step_count"] + 1,
                agent=AgentRole.RETRIEVAL,
                action=AgentAction(plan.action.value),
                thought_summary=plan.reason,
                tool_calls=plan.tool_calls,
                latency_ms=latency_ms,
                tokens_used=tokens,
                stop_reason="RETRIEVAL_STOP" if not plan.tool_calls else None,
            )
        ],
    }


def _pre_tool_guardrail_node(
    state: ApprovalState, runtime: Runtime[ApprovalRuntime]
) -> dict[str, object]:
    latest_supervisor = (
        state.get("supervisor_decisions", [])[-1] if state.get("supervisor_decisions") else None
    )
    plan = state.get("retrieval_plan")
    subject_action = AgentAction(plan.action.value) if plan else AgentAction.RETRIEVE
    violation_reason = hard_limit_violation_reason(state)
    if violation_reason:
        return {
            "status": ApprovalStatus.SYSTEM_ERROR,
            "agent_stop_reason": violation_reason,
            "guardrail_decisions": [
                GuardrailDecision(
                    sequence=len(state.get("guardrail_decisions", [])) + 1,
                    phase="PRE_TOOL",
                    action="VETO",
                    reason=violation_reason,
                    subject_role=AgentRole.RETRIEVAL,
                    subject_action=subject_action,
                    related_supervisor_sequence=(
                        latest_supervisor.sequence if latest_supervisor else None
                    ),
                )
            ],
            "risk_flags": ["GUARDRAIL_HARD_LIMIT_VIOLATION"],
        }

    allowed = set(runtime.context.tool_context.allowed_tools)
    calls = state.get("selected_tools", [])
    if any(call.tool_name not in allowed for call in calls):
        return {
            "status": ApprovalStatus.SYSTEM_ERROR,
            "agent_stop_reason": "UNAUTHORIZED_TOOL",
            "guardrail_decisions": [
                GuardrailDecision(
                    sequence=len(state.get("guardrail_decisions", [])) + 1,
                    phase="PRE_TOOL",
                    action="VETO",
                    reason="UNAUTHORIZED_TOOL",
                    subject_role=AgentRole.RETRIEVAL,
                    subject_action=subject_action,
                    related_supervisor_sequence=(
                        latest_supervisor.sequence if latest_supervisor else None
                    ),
                )
            ],
            "risk_flags": ["AGENT_POLICY_VIOLATION"],
        }

    seen_hashes = {item.call_hash for item in state.get("tool_observations", []) if item.call_hash}
    filtered = []
    duplicate_found = False
    for call in calls:
        call_hash = tool_call_hash(call, runtime.context.tool_context)
        if call_hash not in seen_hashes:
            seen_hashes.add(call_hash)
            filtered.append(call)
        else:
            duplicate_found = True
    update: dict[str, object] = {
        "selected_tools": filtered,
        **(
            {"no_progress_rounds": state["no_progress_rounds"] + 1}
            if calls and not filtered
            else {}
        ),
    }
    if duplicate_found:
        update.update(
            guardrail_decisions=[
                GuardrailDecision(
                    sequence=len(state.get("guardrail_decisions", [])) + 1,
                    phase="PRE_TOOL",
                    action="VETO",
                    reason="DUPLICATE_TOOL_CALL",
                    subject_role=AgentRole.RETRIEVAL,
                    subject_action=subject_action,
                    related_supervisor_sequence=(
                        latest_supervisor.sequence if latest_supervisor else None
                    ),
                )
            ],
            risk_flags=["DUPLICATE_TOOL_CALL_BLOCKED"],
        )
    return update


def _tools_node(state: ApprovalState, runtime: Runtime[ApprovalRuntime]) -> dict[str, object]:
    calls = state.get("selected_tools", [])
    known_ids = {item.evidence_id for item in state.get("evidence", [])}
    observations: list[ToolObservation | None] = [None] * len(calls)
    if calls:
        lanes: dict[object, list[tuple[int, object]]] = {}
        for index, call in enumerate(calls):
            lanes.setdefault(call.tool_name, []).append((index, call))

        def run_lane(items: list[tuple[int, object]]) -> list[tuple[int, ToolObservation]]:
            return [
                (
                    index,
                    runtime.context.registry.execute(
                        call,
                        runtime.context.tool_context,
                        known_evidence_ids=known_ids,
                    ),
                )
                for index, call in items
            ]

        with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
            for lane_results in pool.map(run_lane, lanes.values()):
                for index, observation in lane_results:
                    observations[index] = observation

    completed = [item for item in observations if item is not None]
    new_items = {
        item.evidence_id: item
        for observation in completed
        for item in observation.items
        if item.evidence_id not in known_ids
    }
    # Reference-only case/rule results may guide the next search, but are not
    # authoritative progress for the Reviewer or the NO_NEW_EVIDENCE budget.
    reviewer_usable_items = valid_evidence(
        {**state, "evidence": list(new_items.values())},
        runtime.context.tool_context,
    )
    failed = (
        bool(completed)
        and not any(item.error is None for item in completed)
        and any(item.error != "TOOL_NOT_IMPLEMENTED" for item in completed)
    )
    degraded = any(item.is_degraded for item in completed)
    _inject_failure(runtime, "after_tools")
    return {
        "tool_observations": completed,
        "retrieved_candidates": list(new_items.values()),
        "evidence": list(new_items.values()),
        "risk_flags": ["TOOL_DEGRADED"] if degraded else [],
        "no_progress_rounds": (0 if reviewer_usable_items else state["no_progress_rounds"] + 1),
        **(
            {
                "status": ApprovalStatus.SYSTEM_ERROR,
                "agent_stop_reason": "TOOL_FAILURE",
                "technical_retry_count": state["technical_retry_count"] + 1,
            }
            if failed
            else {}
        ),
    }


def _reviewer_node(state: ApprovalState, runtime: Runtime[ApprovalRuntime]) -> dict[str, object]:
    _inject_failure(runtime, "before_agent")
    try:
        review, tokens, latency_ms, repair_count = review_evidence(
            runtime.context.model,
            state,
            runtime.context.tool_context,
        )
    except Exception as exc:  # noqa: BLE001 - model/schema failures become a controlled state
        LOGGER.warning("Evidence Reviewer failed: %s", type(exc).__name__)
        return _failure(state, "evidence_reviewer")
    _inject_failure(runtime, "after_agent")
    selected_count = len(state.get("selected_tools", []))
    recent_observations = (
        state.get("tool_observations", [])[-selected_count:] if selected_count else []
    )
    return {
        "evidence_review": review,
        "agent_step_count": state["agent_step_count"] + 1,
        "technical_retry_count": state["technical_retry_count"] + repair_count,
        "tokens_used": state["tokens_used"] + tokens,
        "agent_trajectory": [
            AgentStep(
                step_index=state["agent_step_count"] + 1,
                agent=AgentRole.EVIDENCE_REVIEWER,
                action=AgentAction(review.recommended_action.value),
                thought_summary=review.reason,
                observation_ids=[
                    item.call_id or item.call_hash or item.tool_name.value
                    for item in recent_observations
                ],
                new_evidence_ids=[
                    evidence_id
                    for item in recent_observations
                    for evidence_id in item.new_evidence_ids
                ],
                latency_ms=latency_ms,
                tokens_used=tokens,
            )
        ],
    }


def _post_review_guardrail_node(state: ApprovalState) -> dict[str, object]:
    review = state["evidence_review"]
    violation_reason = hard_limit_violation_reason(state)
    if violation_reason is None:
        return {}
    return {
        "status": ApprovalStatus.SYSTEM_ERROR,
        "agent_stop_reason": violation_reason,
        "guardrail_decisions": [
            GuardrailDecision(
                sequence=len(state.get("guardrail_decisions", [])) + 1,
                phase="POST_REVIEW",
                action="STOP",
                reason=violation_reason,
                subject_role=AgentRole.EVIDENCE_REVIEWER,
                subject_action=AgentAction(review.recommended_action.value),
            )
        ],
        "risk_flags": ["GUARDRAIL_HARD_LIMIT_VIOLATION"],
    }


def _route_supervisor(state: ApprovalState) -> str:
    if state["status"] == ApprovalStatus.SYSTEM_ERROR:
        return "end"
    action = state["supervisor_decision"].action
    if action in {
        SupervisorAction.RETRIEVE,
        SupervisorAction.CHALLENGE_AND_RETRIEVE,
    }:
        return "retrieve"
    if action == SupervisorAction.VALIDATE:
        return "validate"
    if action == SupervisorAction.ESCALATE:
        return "human_validate"
    if action == SupervisorAction.REQUEST_DOCUMENTS:
        return "human_validate"
    return "end"


def _route_retrieval(state: ApprovalState) -> str:
    if state["status"] == ApprovalStatus.SYSTEM_ERROR:
        return "end"
    return "guard" if state.get("selected_tools") else "review"


def _route_pre_tool_guardrail(state: ApprovalState) -> str:
    if state["status"] == ApprovalStatus.SYSTEM_ERROR:
        return "end"
    return "tools" if state.get("selected_tools") else "review"


def _route_tools(state: ApprovalState) -> str:
    return "end" if state["status"] == ApprovalStatus.SYSTEM_ERROR else "review"


def _route_reviewer(state: ApprovalState) -> str:
    return "end" if state["status"] == ApprovalStatus.SYSTEM_ERROR else "guard"


def _route_post_review_guardrail(state: ApprovalState) -> str:
    return "end" if state["status"] == ApprovalStatus.SYSTEM_ERROR else "supervisor"


def _rule_evaluation_update(
    state: ApprovalState, runtime: Runtime[ApprovalRuntime]
) -> dict[str, object]:
    evaluation = evaluate_rules(
        state,
        runtime.context.database,
        runtime.context.tool_context,
    )
    return {
        "rule_results": evaluation.results,
        "evidence": evaluation.new_evidence,
        "risk_flags": evaluation.risk_flags,
        "evidence_metrics": evaluation.metrics,
    }


def _rule_validator_node(
    state: ApprovalState, runtime: Runtime[ApprovalRuntime]
) -> dict[str, object]:
    try:
        return _rule_evaluation_update(state, runtime)
    except Exception:  # noqa: BLE001 - automatic decisions require working rules
        return _failure(state, "rule_validator")


def _human_rule_validator_node(
    state: ApprovalState, runtime: Runtime[ApprovalRuntime]
) -> dict[str, object]:
    try:
        return _rule_evaluation_update(state, runtime)
    except Exception:  # noqa: BLE001 - never overwrite an established human escalation
        return {"risk_flags": ["RULE_VALIDATOR_DEGRADED"]}


def _risk_router_node(state: ApprovalState) -> dict[str, object]:
    return route_risk(state)


def allowed_human_actions(state: ApprovalState) -> list[HumanReviewAction]:
    pending = state.get("pending_human_action")
    if pending and pending.action == HumanActionType.REQUEST_DOCUMENTS:
        return [HumanReviewAction.EDIT, HumanReviewAction.REJECT]
    return [
        HumanReviewAction.APPROVE,
        HumanReviewAction.EDIT,
        HumanReviewAction.REJECT,
    ]


def validate_human_submission(
    state: ApprovalState, submission: HumanReviewSubmission
) -> Recommendation:
    if submission.action not in allowed_human_actions(state):
        raise PermissionError("human action is not allowed for the pending review")
    recommendation = {
        HumanReviewAction.APPROVE: Recommendation.PASS_RECOMMENDED,
        HumanReviewAction.REJECT: Recommendation.REJECT_RECOMMENDED,
    }.get(submission.action, submission.recommendation)
    if recommendation is None:
        raise ValueError("human review did not produce a final recommendation")
    pending = state.get("pending_human_action")
    if (
        pending
        and pending.action == HumanActionType.REQUEST_DOCUMENTS
        and recommendation == Recommendation.PASS_RECOMMENDED
    ):
        raise PermissionError("missing-document reviews cannot produce approval")
    return recommendation


def _human_review_node(
    state: ApprovalState, runtime: Runtime[ApprovalRuntime]
) -> dict[str, object]:
    pending = state.get("pending_human_action")
    payload = HumanReviewPayload(
        request_id=state["request_id"],
        trace_id=state["trace_id"],
        recommendation=state.get("recommendation"),
        risk_level=state.get("risk_level"),
        reasons=state.get("decision_reasons", []),
        rule_results=state.get("rule_results", []),
        evidence=state.get("evidence", []),
        missing_items=pending.missing_items if pending else [],
        allowed_actions=allowed_human_actions(state),
    )
    if runtime.context.persist_workflow:
        runtime.context.database.mark_human_pending(
            request_id=state["request_id"],
            status=state["status"],
            recommendation=state["recommendation"],
            risk_level=state.get("risk_level"),
            missing_items=payload.missing_items,
            allowed_actions=[item.value for item in payload.allowed_actions],
        )
    submission = HumanReviewSubmission.model_validate(interrupt(payload.model_dump(mode="json")))
    recommendation = validate_human_submission(state, submission)
    final_decision = FinalDecision(
        action=submission.action,
        operator_id=submission.operator_id,
        recommendation=recommendation,
        reason=submission.reason,
        previous_recommendation=state.get("recommendation"),
    )
    return {
        "status": ApprovalStatus.COMPLETED,
        "recommendation": recommendation,
        "decision_reasons": [
            DecisionReason(
                code=f"HUMAN_{submission.action.value.upper()}",
                message=submission.reason,
            )
        ],
        "pending_human_action": None,
        "final_decision": final_decision,
        "human_review_idempotency_key": submission.idempotency_key,
    }


def _finalize_node(state: ApprovalState, runtime: Runtime[ApprovalRuntime]) -> dict[str, object]:
    _inject_failure(runtime, "before_finalize")
    if runtime.context.persist_workflow:
        runtime.context.database.finalize_approval(
            request_id=state["request_id"],
            status=state["status"],
            recommendation=state.get("recommendation"),
            risk_level=state.get("risk_level"),
            final_decision=state.get("final_decision"),
            human_idempotency_key=state.get("human_review_idempotency_key"),
        )
    _inject_failure(runtime, "after_finalize")
    return {}


def _route_risk_router(state: ApprovalState) -> str:
    return "human" if state.get("recommendation") == Recommendation.HUMAN_REVIEW else "finalize"


def build_approval_graph(checkpointer: BaseCheckpointSaver | None = None):
    """Compile the approval graph with optional P09 durable checkpointing."""

    graph = StateGraph(ApprovalState, context_schema=ApprovalRuntime)
    graph.add_node("supervisor", _supervisor_node)
    graph.add_node("retrieval_agent", _retrieval_node)
    graph.add_node("pre_tool_guardrail", _pre_tool_guardrail_node)
    graph.add_node("tools", _tools_node)
    graph.add_node("evidence_reviewer", _reviewer_node)
    graph.add_node("post_review_guardrail", _post_review_guardrail_node)
    graph.add_node("rule_validator", _rule_validator_node)
    graph.add_node("human_rule_validator", _human_rule_validator_node)
    graph.add_node("risk_router", _risk_router_node)
    # Persistence errors remain on the same checkpointed node. Exhausted retries
    # propagate to the API as a technical failure instead of advancing the graph.
    graph.add_node(
        "human_review",
        _human_review_node,
        retry_policy=_PERSISTENCE_RETRY_POLICY,
    )
    graph.add_node("finalize", _finalize_node, retry_policy=_PERSISTENCE_RETRY_POLICY)
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        _route_supervisor,
        {
            "retrieve": "retrieval_agent",
            "validate": "rule_validator",
            "human_validate": "human_rule_validator",
            "human_finalize": "risk_router",
            "end": "finalize",
        },
    )
    graph.add_conditional_edges(
        "retrieval_agent",
        _route_retrieval,
        {
            "guard": "pre_tool_guardrail",
            "review": "evidence_reviewer",
            "end": "finalize",
        },
    )
    graph.add_conditional_edges(
        "pre_tool_guardrail",
        _route_pre_tool_guardrail,
        {
            "tools": "tools",
            "review": "evidence_reviewer",
            "end": "finalize",
        },
    )
    graph.add_conditional_edges(
        "tools", _route_tools, {"review": "evidence_reviewer", "end": "finalize"}
    )
    graph.add_conditional_edges(
        "evidence_reviewer",
        _route_reviewer,
        {"guard": "post_review_guardrail", "end": "finalize"},
    )
    graph.add_conditional_edges(
        "post_review_guardrail",
        _route_post_review_guardrail,
        {"supervisor": "supervisor", "end": "finalize"},
    )
    graph.add_edge("rule_validator", "risk_router")
    graph.add_edge("human_rule_validator", "risk_router")
    graph.add_conditional_edges(
        "risk_router",
        _route_risk_router,
        {"human": "human_review", "finalize": "finalize"},
    )
    graph.add_edge("human_review", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer, name="p09-approval-human-review")
