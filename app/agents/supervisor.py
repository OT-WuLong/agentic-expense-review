"""Supervisor Agent: own global routing, retrieval strategy, and one bounded challenge."""

from __future__ import annotations

from app.agents.contracts import (
    EvidenceAction,
    EvidenceReview,
    SupervisorAction,
    SupervisorDecision,
)
from app.agents.model import StructuredChatClient
from app.graph.budget import evaluate_round_budget
from app.graph.state import ApprovalState

SUPERVISOR_PROMPT_VERSION = "p08-supervisor-v3"
MAX_SUPERVISOR_CHALLENGES = 1

_SYSTEM_PROMPT = """你是费用预审 Supervisor，Evidence Reviewer 的结论只是建议，由你决定下一阶段。
你不调用工具、不做最终审批。Reviewer 要求补检时，比较明确缺口、历史失败、证据增量和剩余预算：
值得继续则选择 RETRIEVE 并生成不同于旧目标的新 retrieval_goal；缺少申请人材料选择
REQUEST_DOCUMENTS；继续检索边际收益很低或冲突无法消除时选择 ESCALATE。Reviewer 判断
SUFFICIENT 时通常选择 VALIDATE；只有能指出具体遗漏问题时才能选择 CHALLENGE_AND_RETRIEVE，
且必须填写 challenged_question_ids 和新的 retrieval_goal。非检索动作必须填写 stop_reason。
存在 tool_history 时至少引用一个相关 call_id，且 referenced_call_ids 只能来自输入。
系统错误不得解释成业务拒绝。"""


def _tool_history(state: ApprovalState) -> list[dict[str, object]]:
    calls = {
        call.call_id: call
        for step in state.get("agent_trajectory", [])
        for call in step.tool_calls
        if call.call_id
    }
    return [
        {
            "call_id": observation.call_id,
            "tool_name": observation.tool_name.value,
            "query": observation.query,
            "purpose": (
                calls[observation.call_id].purpose if observation.call_id in calls else None
            ),
            "error": observation.error,
            "is_degraded": observation.is_degraded,
            "returned_evidence_count": len(observation.items),
            "new_evidence_count": len(observation.new_evidence_ids),
        }
        for observation in state.get("tool_observations", [])[-8:]
    ]


def _deterministic(
    *,
    action: SupervisorAction,
    reason: str,
    stop_reason: str | None = None,
    retrieval_goal: str | None = None,
    call_ids: list[str] | None = None,
) -> tuple[SupervisorDecision, int, int, bool, int]:
    return (
        SupervisorDecision(
            action=action,
            reason=reason,
            retrieval_goal=retrieval_goal,
            stop_reason=stop_reason,
            referenced_call_ids=call_ids or [],
        ),
        0,
        0,
        False,
        0,
    )


def _canonical_stop_reason(action: SupervisorAction, review: EvidenceReview) -> str:
    if action == SupervisorAction.VALIDATE:
        return "ALL_NECESSARY_EVIDENCE_COVERED"
    if action == SupervisorAction.REQUEST_DOCUMENTS:
        return "REQUEST_DOCUMENTS"
    if action == SupervisorAction.ESCALATE:
        return (
            "UNRESOLVED_POLICY_VERSION_CONFLICT"
            if review.conflict_evidence_ids
            else "EVIDENCE_ESCALATION"
        )
    raise ValueError(f"no canonical stop reason for {action}")


def decide_reviewer_direct(
    state: ApprovalState,
) -> tuple[SupervisorDecision, int, int, bool, int]:
    """Ablation: route the Reviewer's advice without an LLM Supervisor."""

    review = state.get("evidence_review")
    call_ids = [str(item["call_id"]) for item in _tool_history(state) if item["call_id"]]
    if review is None:
        budget = evaluate_round_budget(state)
        if not budget.can_start_round:
            return _deterministic(
                action=SupervisorAction.ESCALATE,
                reason="首次检索预算不足。",
                stop_reason=budget.start_block_reason,
            )
        return _deterministic(
            action=SupervisorAction.RETRIEVE,
            reason="开始首次检索。",
            retrieval_goal="核实适用制度和业务事实。",
        )
    if review.recommended_action == EvidenceAction.SUFFICIENT:
        action = SupervisorAction.VALIDATE
    elif review.recommended_action == EvidenceAction.REQUEST_DOCUMENTS:
        action = SupervisorAction.REQUEST_DOCUMENTS
    elif review.recommended_action == EvidenceAction.ESCALATE:
        action = SupervisorAction.ESCALATE
    else:
        budget = evaluate_round_budget(state)
        if budget.can_start_round:
            return _deterministic(
                action=SupervisorAction.RETRIEVE,
                reason=review.reason,
                retrieval_goal=(
                    f"第{state['retrieval_round_count'] + 1}轮核实："
                    + "；".join(review.evidence_gaps or [review.reason])
                )[:500],
                call_ids=call_ids,
            )
        return _deterministic(
            action=SupervisorAction.ESCALATE,
            reason="Reviewer 要求补检，但剩余预算不足。",
            stop_reason=budget.start_block_reason or "MARGINAL_RETRIEVAL_BUDGET_EXHAUSTED",
            call_ids=call_ids,
        )
    return _deterministic(
        action=action,
        reason=review.reason,
        stop_reason=_canonical_stop_reason(action, review),
        call_ids=call_ids,
    )


def decide_supervisor(
    client: StructuredChatClient, state: ApprovalState
) -> tuple[SupervisorDecision, int, int, bool, int]:
    """Choose the final route; deterministic decisions are still audited by the graph."""

    review = state.get("evidence_review")
    history = _tool_history(state)
    call_ids = [str(item["call_id"]) for item in history if item["call_id"]]
    round_budget = evaluate_round_budget(state)
    if review is None:
        if not round_budget.can_start_round:
            return _deterministic(
                action=SupervisorAction.ESCALATE,
                reason="当前预算不足以完成一轮检索与证据复核。",
                stop_reason=round_budget.start_block_reason,
                call_ids=call_ids,
            )
        return _deterministic(
            action=SupervisorAction.RETRIEVE,
            reason="尚无证据评审结果，进入首次检索。",
            retrieval_goal="核实适用制度、关键结构化事实、例外和潜在版本冲突。",
        )

    recommendation = review.recommended_action
    if recommendation == EvidenceAction.REQUEST_DOCUMENTS:
        return _deterministic(
            action=SupervisorAction.REQUEST_DOCUMENTS,
            reason=review.reason,
            stop_reason=_canonical_stop_reason(SupervisorAction.REQUEST_DOCUMENTS, review),
            call_ids=call_ids,
        )
    if recommendation == EvidenceAction.ESCALATE:
        return _deterministic(
            action=SupervisorAction.ESCALATE,
            reason=review.reason,
            stop_reason=_canonical_stop_reason(SupervisorAction.ESCALATE, review),
            call_ids=call_ids,
        )

    can_retrieve = round_budget.can_start_round
    if recommendation == EvidenceAction.SUFFICIENT and (
        state["supervisor_challenge_count"] >= MAX_SUPERVISOR_CHALLENGES or not can_retrieve
    ):
        return _deterministic(
            action=SupervisorAction.VALIDATE,
            reason="证据评审充分，且挑战机会已使用或剩余预算不足。",
            stop_reason=_canonical_stop_reason(SupervisorAction.VALIDATE, review),
            call_ids=call_ids,
        )
    if recommendation == EvidenceAction.RETRIEVE_MORE and not can_retrieve:
        return _deterministic(
            action=SupervisorAction.ESCALATE,
            reason="继续检索所需预算不足或连续检索没有新增证据。",
            stop_reason=(round_budget.start_block_reason or "MARGINAL_RETRIEVAL_BUDGET_EXHAUSTED"),
            call_ids=call_ids,
        )

    allowed_actions = (
        {SupervisorAction.VALIDATE, SupervisorAction.CHALLENGE_AND_RETRIEVE}
        if recommendation == EvidenceAction.SUFFICIENT
        else {
            SupervisorAction.RETRIEVE,
            SupervisorAction.REQUEST_DOCUMENTS,
            SupervisorAction.ESCALATE,
        }
    )
    selected_count = len(state.get("selected_tools", []))
    latest_history = history[-selected_count:] if selected_count else []
    payload: dict[str, object] = {
        "prompt_version": SUPERVISOR_PROMPT_VERSION,
        "application": state["application"].model_dump(mode="json", exclude_none=True),
        "current_retrieval_goal": state.get("retrieval_goal"),
        "reviewer_recommendation": review.model_dump(mode="json", exclude_none=True),
        "open_questions": [
            item.model_dump(mode="json") for item in state.get("open_questions", [])
        ],
        "tool_history": history,
        "evidence_summary": {
            "total": len(state.get("evidence", [])),
            "retrieved": len(state.get("retrieved_candidates", [])),
            "latest_new": sum(int(item["new_evidence_count"]) for item in latest_history),
            "gaps": review.evidence_gaps,
            "conflict_evidence_ids": review.conflict_evidence_ids,
        },
        "budget": round_budget.payload(),
        "supervisor_challenge_count": state["supervisor_challenge_count"],
        "allowed_actions": sorted(item.value for item in allowed_actions),
    }
    known_call_ids = set(call_ids)
    known_question_ids = {item.question_id for item in state.get("open_questions", [])}
    total_tokens = 0
    total_latency_ms = 0
    for attempt in range(2):
        try:
            decision, tokens, latency_ms = client.generate(
                SupervisorDecision,
                system_prompt=_SYSTEM_PROMPT,
                payload=payload,
            )
            total_tokens += tokens
            total_latency_ms += latency_ms
            if decision.action not in allowed_actions:
                raise ValueError("Supervisor selected an action outside allowed_actions")
            if not set(decision.referenced_call_ids) <= known_call_ids:
                raise ValueError("Supervisor referenced an unknown call ID")
            if history and not decision.referenced_call_ids:
                raise ValueError("Supervisor must cite at least one visible call ID")
            if not set(decision.challenged_question_ids) <= known_question_ids:
                raise ValueError("Supervisor challenged an unknown question ID")
            if decision.action in {
                SupervisorAction.RETRIEVE,
                SupervisorAction.CHALLENGE_AND_RETRIEVE,
            } and "".join((decision.retrieval_goal or "").split()) == "".join(
                (state.get("retrieval_goal") or "").split()
            ):
                raise ValueError("Supervisor retrieval goal must change after review")
            if decision.action not in {
                SupervisorAction.RETRIEVE,
                SupervisorAction.CHALLENGE_AND_RETRIEVE,
            }:
                decision = decision.model_copy(
                    update={"stop_reason": _canonical_stop_reason(decision.action, review)}
                )
            return decision, total_tokens, total_latency_ms, True, attempt
        except ValueError:
            if attempt == 1:
                raise
            payload["repair_feedback"] = {
                "instruction": (
                    "只从 allowed_actions 选择；引用现有 call/question ID；检索时生成不同于旧目标"
                    "的新 retrieval_goal；非检索动作填写 stop_reason。"
                )
            }
    raise AssertionError("unreachable supervisor retry state")
