"""Deterministic risk routing after P08 rule validation."""

from __future__ import annotations

from app.graph.state import ApprovalState
from app.models import (
    ApprovalStatus,
    DecisionReason,
    HumanActionType,
    Recommendation,
    RiskLevel,
    RuleOutcome,
)

_MANUAL_FLAGS = {
    "RULE_VERSION_CONFLICT",
    "MISSING_AUTHORITATIVE_PRECEDENCE",
    "OVERDUE_EXCEPTION_REQUIRED",
    "LODGING_EXCEPTION_REQUIRES_REVIEW",
}
_HARD_PROHIBITIONS = {
    "CONFIRMED_PRIVATE_COMMUTE",
    "DAILY_COMMUTE_EXPLICITLY_PROHIBITED",
}


def route_risk(state: ApprovalState) -> dict[str, object]:
    if state["status"] == ApprovalStatus.SYSTEM_ERROR:
        return {"recommendation": None, "risk_level": None, "decision_reasons": []}

    results = state.get("rule_results", [])
    failed = [item for item in results if item.outcome == RuleOutcome.FAIL]
    indeterminate = [item for item in results if item.outcome == RuleOutcome.INDETERMINATE]
    pending = state.get("pending_human_action")
    confirmed_mismatch = any(
        item.reason_code == "DOCUMENT_APPLICATION_MISMATCH" for item in failed
    )
    # 明确禁报无需补件；其他规则失败仍可由 Reviewer 请求人工核实。
    hard_prohibition = any(item.reason_code in _HARD_PROHIBITIONS for item in failed)
    if (
        pending
        and pending.action == HumanActionType.REQUEST_DOCUMENTS
        and not (confirmed_mismatch or hard_prohibition)
    ):
        return {
            "status": ApprovalStatus.INSUFFICIENT_EVIDENCE,
            "recommendation": Recommendation.HUMAN_REVIEW,
            "risk_level": RiskLevel.MEDIUM,
            "decision_reasons": [
                DecisionReason(
                    code="MISSING_DOCUMENTS",
                    message=pending.reason,
                )
            ],
        }

    duplicate_invoice = any(item.reason_code == "DUPLICATE_INVOICE_FOUND" for item in failed)
    manual_flag = bool(_MANUAL_FLAGS & set(state.get("risk_flags", [])))
    forced_human = not hard_prohibition and (
        bool(pending and pending.action != HumanActionType.REQUEST_DOCUMENTS)
        or manual_flag
        or duplicate_invoice
    )
    if forced_human:
        relevant = [*indeterminate, *failed] if duplicate_invoice else indeterminate or failed
        high_risk = bool(failed) or manual_flag
        return {
            "status": ApprovalStatus.HUMAN_PENDING,
            "recommendation": Recommendation.HUMAN_REVIEW,
            "risk_level": RiskLevel.HIGH if high_risk else RiskLevel.MEDIUM,
            "decision_reasons": [
                DecisionReason(
                    code="MANUAL_REVIEW_REQUIRED" if high_risk else "INSUFFICIENT_EVIDENCE",
                    message=(
                        "票据号已出现在更早的上传申请中，存在重复报销风险，需人工核对。"
                        if duplicate_invoice
                        else "存在需要人工确认的制度冲突、例外事项或规则失败。"
                        if high_risk
                        else "票据或制度证据不足，需人工核实；尚未判定违规。"
                    ),
                    evidence_ids=list(
                        dict.fromkeys(
                            evidence_id
                            for item in relevant
                            for evidence_id in item.evidence_ids
                        )
                    ),
                    rule_ids=[item.rule_id for item in relevant],
                )
            ],
        }
    if failed:
        message = "申请命中明确的不予报销或额度规则。"
        if confirmed_mismatch and pending:
            message = "票据与申请的已识别字段不一致；确定性规则覆盖了补件建议。"
        if hard_prohibition:
            message = "票据行程已确认属于制度禁止的私人通勤；补件建议不改变禁报结论。"
        return {
            "status": ApprovalStatus.BUSINESS_REJECTED,
            "recommendation": Recommendation.REJECT_RECOMMENDED,
            "risk_level": RiskLevel.HIGH,
            "pending_human_action": None,
            "decision_reasons": [
                DecisionReason(
                    code="HARD_RULE_FAILED",
                    message=message,
                    evidence_ids=list(
                        dict.fromkeys(
                            evidence_id for item in failed for evidence_id in item.evidence_ids
                        )
                    ),
                    rule_ids=[item.rule_id for item in failed],
                )
            ],
        }
    coverage = state.get("evidence_metrics")
    if indeterminate or (coverage and coverage.evidence_coverage not in {None, 1.0}):
        return {
            "status": ApprovalStatus.INSUFFICIENT_EVIDENCE,
            "recommendation": Recommendation.HUMAN_REVIEW,
            "risk_level": RiskLevel.MEDIUM,
            "decision_reasons": [
                DecisionReason(
                    code="INSUFFICIENT_EVIDENCE",
                    message="现有材料不足以完成全部确定性规则判断。",
                    evidence_ids=list(
                        dict.fromkeys(
                            evidence_id
                            for item in indeterminate
                            for evidence_id in item.evidence_ids
                        )
                    ),
                    rule_ids=[item.rule_id for item in indeterminate],
                )
            ],
        }
    return {
        "status": ApprovalStatus.COMPLETED,
        "recommendation": Recommendation.PASS_RECOMMENDED,
        "risk_level": RiskLevel.LOW,
        "decision_reasons": [
            DecisionReason(
                code="ALL_RULES_PASSED",
                message="检索证据充分，全部适用的确定性规则通过。",
                evidence_ids=list(
                    dict.fromkeys(
                        evidence_id for item in results for evidence_id in item.evidence_ids
                    )
                ),
                rule_ids=[item.rule_id for item in results],
            )
        ],
    }
