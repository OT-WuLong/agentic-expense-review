"""Evidence Reviewer: deterministic provenance gate plus semantic coverage review."""

from __future__ import annotations

import re

from app.agents.contracts import (
    CoverageStatus,
    EvidenceAction,
    EvidenceReview,
    QuestionCoverage,
    ToolName,
)
from app.agents.model import StructuredChatClient
from app.database import PostgresStore
from app.graph.state import ApprovalState
from app.models import EvidenceItem, EvidenceSource
from app.rules import policy_rule_checks
from app.tools.registry import ToolExecutionContext

EVIDENCE_REVIEW_PROMPT_VERSION = "p15-evidence-review-grounding-v5"
REVIEWER_FORMAT_ATTEMPTS = 3

_SYSTEM_PROMPT = """你是 Evidence Reviewer，只判断现有证据能否覆盖本轮子问题并给出 recommended_action；
最终阶段路由由 Supervisor 决定。
制度证据必须来自正确目录快照、已发布版本且覆盖费用发生日；结构化证据必须来自授权快照。
历史案例和规则目录只供检索规划参考，不可单独证明制度适用或替代正式规则结果。
每个判断只能引用 provided_evidence_ids 中的短标签，须原样写成 JSON 字符串，不要写数字或长哈希。
缺失但可继续检索时输出 RETRIEVE_MORE；缺少申请人材料或票据关键字段不可读时输出
REQUEST_DOCUMENTS。若申请和票据的金额、日期、人数或路线都明确却不一致，这是规则可判定的业务事实，
不是证据缺口：Q-RECEIPT 标为 SUPPORTED，在 reason 写明差异；其他问题也已覆盖时输出 SUFFICIENT，
交由规则引擎决定驳回或转人工。不得因猜测将来可能提交更正票据，就把当前明确的不一致改判补件。
无法通过权威元数据解决的制度冲突输出 ESCALATE；全部问题均有充分证据且无制度冲突才输出
SUFFICIENT。不同额度不等于本单存在实质冲突：在每个适用版本下分别核算本单，均在限额内或均超限时，
引用全部适用额度并标 SUPPORTED，交给规则引擎，不需选择唯一版本，也不得因此补检或转人工。
只有适用版本会给本单带来不同结果时才构成实质额度冲突。
若两个同时有效、权威等级和优先级相同、互不替代的制度产生这种不同结果，
不得自行选择较新版本：尚未定向查询替代关系、优先级或过渡条款时先 RETRIEVE_MORE；已经
补检仍没有权威依据时才列出冲突证据并 ESCALATE。若通用制度与适用部门细则的 priority 不同，
同一额度以较高 priority 为准，不得称为制度版本冲突；会议通知等材料缺失应 REQUEST_DOCUMENTS。
部门、日期和权限已经由服务端过滤；不能仅因
未发现部门特例就制造证据缺口。已有结构化等级或类别时，应检查基础制度是否给出了该具体值对应
的计算标准；只有证据明确暗示存在覆盖制度时，才要求检索部门例外。不得输出最终审批建议。
questions 是服务端按已发布目录确定的必核清单，不得增加清单外问题；rule_scope_complete 为 true 时，
applicable_rule_ids 是本单必核规则的完整集合。若集合没有出租车限额规则，不能因为没有找到
出租车限额、超限例外或审批条款而判 MISSING。Q-EXCEPTION 只核对制度是否写明例外通道；
找到条款就引用原文判 SUPPORTED，未提交会议通知等实际材料则另列 evidence_gaps 并请求补件。
policy_rule_checks 列出服务端已发布规则的参数与原文引用；参数本身不是证据，evidence_ids 为空表示
还没有检出必要条款。Q-POLICY 必须逐条核对必要额度和提交时限的具体参数，泛泛提及提交的总则
不能证明 30/45 天期限。缺这种条款应定向 RETRIEVE_MORE，不是向申请人要求新的制度文件。
amount_within_limit 是用票据人数和申请金额核算的辅助结果，不代替你核对票据与申请是否一致。
只检查实际制度和票据证据对 questions 的覆盖。"""


def _evidence_payload(item: EvidenceItem) -> dict[str, object]:
    payload = item.model_dump(mode="json", exclude_none=True)
    if item.excerpt:
        payload["excerpt"] = item.excerpt[:1200]
    if item.source_type in {
        EvidenceSource.POLICY_DOCUMENT,
        EvidenceSource.DOCUMENT_CONTEXT,
    }:
        payload["effective_to"] = item.effective_to.isoformat() if item.effective_to else None
        payload["supersedes_document_id"] = item.supersedes_document_id
    return payload


def valid_evidence(state: ApprovalState, context: ToolExecutionContext) -> list[EvidenceItem]:
    """Return citable evidence; case memory and rule listings remain planning context."""

    occurred_on = state["application"].occurred_on
    document_ids = {item.document_id for item in state.get("documents", [])}
    valid: list[EvidenceItem] = []
    for item in state.get("evidence", []):
        if item.source_type == EvidenceSource.ATTACHMENT or (
            item.source_type == EvidenceSource.DOCUMENT_CONTEXT and item.document_id in document_ids
        ):
            if item.document_id in document_ids:
                valid.append(item)
            continue
        if item.source_type == EvidenceSource.STRUCTURED_RECORD:
            if item.structured_data_snapshot_id in context.structured_snapshots:
                valid.append(item)
            continue
        if item.source_type in {
            EvidenceSource.POLICY_DOCUMENT,
            EvidenceSource.DOCUMENT_CONTEXT,
        } and (
            item.document_id in context.allowed_document_ids
            and item.catalog_snapshot_id in context.policy_snapshots
            and item.published_status == "PUBLISHED"
            and item.effective_from is not None
            and item.effective_from <= occurred_on
            and (item.effective_to is None or occurred_on <= item.effective_to)
        ):
            valid.append(item)
    return list({item.evidence_id: item for item in valid}.values())


def _resolve_priority_conflict(
    review: EvidenceReview, evidence_by_alias: dict[str, EvidenceItem], expense_type: str
) -> EvidenceReview:
    """A lower-priority general clause is not an unresolved department-policy conflict."""

    if (
        review.recommended_action not in {EvidenceAction.ESCALATE, EvidenceAction.RETRIEVE_MORE}
        or len(review.conflict_evidence_ids) < 2
    ):
        return review
    conflicting = [evidence_by_alias[alias] for alias in review.conflict_evidence_ids]
    if (
        any(
            item.source_type
            not in {EvidenceSource.POLICY_DOCUMENT, EvidenceSource.DOCUMENT_CONTEXT}
            or item.priority is None
            or not item.authority_level
            for item in conflicting
        )
        or len({item.authority_level for item in conflicting}) != 1
    ):
        return review
    conflict_priority = max(item.priority for item in conflicting if item.priority is not None)

    def has_expense_limit(item: EvidenceItem) -> bool:
        return (
            expense_type in f"{item.section or ''} {item.excerpt or ''}"
            and bool(
                re.search(
                    r"(?:上限|最高|不超过)[^。；\n]{0,50}\d+(?:\.\d+)?\s*元",
                    item.excerpt or "",
                )
            )
        )

    higher = {
        alias: item
        for alias, item in evidence_by_alias.items()
        if item.source_type == EvidenceSource.POLICY_DOCUMENT
        and item.authority_level == conflicting[0].authority_level
        and item.priority is not None
        and item.priority > conflict_priority
    }
    # A cover page proves scope and priority, but not the actual monetary ceiling.
    higher_limits = {
        alias: item
        for alias, item in higher.items()
        if has_expense_limit(item)
    }
    if higher and not higher_limits:
        return EvidenceReview(
            recommended_action=EvidenceAction.RETRIEVE_MORE,
            reason="已检出更高优先级的适用制度，但尚缺其具体额度条款；不能把低优先级版本差异判为不可消歧。",
            coverage=review.coverage,
            evidence_gaps=[
                f"检索优先级更高的制度 {next(iter(higher.values())).document_id} 的具体额度条款"
            ],
        )
    conflict_aliases = list(dict.fromkeys([*review.conflict_evidence_ids, *higher_limits]))
    highest = max(evidence_by_alias[alias].priority for alias in conflict_aliases)
    winners = {alias for alias in conflict_aliases if evidence_by_alias[alias].priority == highest}
    winner_documents = {evidence_by_alias[alias].document_id for alias in winners}
    if len(winners) == len(conflict_aliases) or len(winner_documents) != 1:
        return review
    if any(has_expense_limit(item) for item in conflicting) and not any(
        has_expense_limit(evidence_by_alias[alias]) for alias in winners
    ):
        return EvidenceReview(
            recommended_action=EvidenceAction.RETRIEVE_MORE,
            reason="已确定更高优先级制度，但尚未取得该制度的具体额度条款。",
            coverage=review.coverage,
            evidence_gaps=[f"检索 {next(iter(winner_documents))} 的具体额度条款"],
        )
    coverage = []
    for item in review.coverage:
        if item.status != CoverageStatus.CONFLICTING or not set(item.evidence_ids).intersection(
            review.conflict_evidence_ids
        ):
            coverage.append(item)
            continue
        citations = [
            alias
            for alias in item.evidence_ids
            if alias not in review.conflict_evidence_ids or alias in winners
        ]
        citations.extend(alias for alias in winners if alias not in citations)
        coverage.append(
            item.model_copy(
                update={
                    "status": CoverageStatus.SUPPORTED,
                    "evidence_ids": citations,
                }
            )
        )
    losing_documents = {
        item.document_id for item in conflicting if item.document_id not in winner_documents
    }
    remaining_gaps = [
        gap
        for gap in review.evidence_gaps
        if not any(document_id and document_id in gap for document_id in losing_documents)
        and not ("版本" in gap and any(term in gap for term in ("衔接", "废止", "替代", "优先")))
    ]
    if remaining_gaps:
        missing = [item for item in coverage if item.status != CoverageStatus.SUPPORTED]
        only_meeting_materials_missing = (
            len(missing) == 1
            and missing[0].question_id == "Q-EXCEPTION"
            and missing[0].status == CoverageStatus.MISSING
        )
        exception_clause_found = any(
            item.source_type == EvidenceSource.POLICY_DOCUMENT
            and "会议主办方指定酒店" in (item.excerpt or "")
            and "批准例外" in (item.excerpt or "")
            for item in evidence_by_alias.values()
        )
        if not (only_meeting_materials_missing and exception_clause_found):
            return EvidenceReview(
                recommended_action=EvidenceAction.RETRIEVE_MORE,
                reason="制度优先级已消歧，仍需处理其余证据缺口。",
                coverage=coverage,
                evidence_gaps=remaining_gaps,
            )
        return EvidenceReview(
            recommended_action=EvidenceAction.REQUEST_DOCUMENTS,
            reason="权威元数据已消歧；会议酒店例外需补会议通知和指定酒店说明，并交有权限人员批准。",
            coverage=coverage,
            evidence_gaps=["会议通知和指定酒店说明尚未提供"],
        )
    if any(item.status != CoverageStatus.SUPPORTED or not item.evidence_ids for item in coverage):
        return EvidenceReview(
            recommended_action=EvidenceAction.RETRIEVE_MORE,
            reason="制度优先级已消歧，其他子问题仍需证据。",
            coverage=coverage,
            evidence_gaps=remaining_gaps,
        )
    return EvidenceReview(
        recommended_action=EvidenceAction.SUFFICIENT,
        reason="权威元数据校正：部门细则优先级高于通用制度，额度冲突可消歧；交由规则校验业务事实。",
        coverage=coverage,
    )


def review_evidence(
    client: StructuredChatClient,
    state: ApprovalState,
    context: ToolExecutionContext,
    *,
    database: PostgresStore | None = None,
) -> tuple[EvidenceReview, int, int, int]:
    """Reject stale/untraceable evidence before asking the model about semantics."""

    questions = state.get("open_questions", [])
    evidence = valid_evidence(state, context)
    if not evidence:
        return (
            EvidenceReview(
                recommended_action=EvidenceAction.RETRIEVE_MORE,
                reason="当前没有通过来源与时效检查的证据。",
                coverage=[
                    QuestionCoverage(
                        question_id=item.question_id,
                        status=CoverageStatus.MISSING,
                    )
                    for item in questions
                ],
                evidence_gaps=[item.text for item in questions],
            ),
            0,
            0,
            0,
        )

    alias_to_id = {
        f"E{index:03d}": item.evidence_id for index, item in enumerate(evidence, start=1)
    }
    evidence_by_alias = dict(zip(alias_to_id, evidence, strict=True))
    id_to_alias = {value: key for key, value in alias_to_id.items()}
    checks = (
        policy_rule_checks({**state, "evidence": evidence}, database, context)
        if database is not None
        else []
    )
    missing_checks = [item for item in checks if not item["evidence_ids"]]
    valid_ids = set(alias_to_id)
    question_ids = {item.question_id for item in questions}
    payload: dict[str, object] = {
        "prompt_version": EVIDENCE_REVIEW_PROMPT_VERSION,
        "application": state["application"].model_dump(mode="json", exclude_none=True),
        "questions": [item.model_dump(mode="json") for item in questions],
        "applicable_policy_ids": context.allowed_document_ids,
        "applicable_rule_ids": context.applicable_rule_ids,
        "rule_scope_complete": context.rule_scope_complete,
        "policy_rule_checks": [
            {**item, "evidence_ids": [id_to_alias[eid] for eid in item["evidence_ids"]]}
            for item in checks
        ],
        "provided_evidence_ids": list(alias_to_id),
        "evidence": [
            {**_evidence_payload(item), "evidence_id": alias}
            for alias, item in zip(alias_to_id, evidence, strict=True)
        ],
        "retrieval_round_count": state["retrieval_round_count"],
        "previous_calls": [
            {
                "tool_name": item.tool_name.value,
                "query": item.query,
                "purpose": next(
                    (
                        call.purpose
                        for step in reversed(state.get("agent_trajectory", []))
                        for call in step.tool_calls
                        if call.call_id == item.call_id
                    ),
                    None,
                ),
                "error": item.error,
            }
            for item in state.get("tool_observations", [])
            if item.tool_name not in {ToolName.CASE_SEARCH, ToolName.RULE_SEARCH}
        ],
    }
    total_tokens = 0
    total_latency_ms = 0
    for attempt in range(REVIEWER_FORMAT_ATTEMPTS):
        try:
            review, tokens, latency_ms = client.generate(
                EvidenceReview,
                system_prompt=_SYSTEM_PROMPT,
                payload=payload,
            )
            total_tokens += tokens
            total_latency_ms += latency_ms
            referenced_ids = {
                evidence_id for item in review.coverage for evidence_id in item.evidence_ids
            } | set(review.conflict_evidence_ids)
            if not referenced_ids <= valid_ids:
                raise ValueError("Evidence Reviewer cited an unknown or invalid Evidence ID")
            if {item.question_id for item in review.coverage} != question_ids:
                raise ValueError(
                    "Evidence Reviewer must cover every current sub-question exactly once"
                )
            review = _resolve_priority_conflict(
                review, evidence_by_alias, state["application"].expense_type.value
            )
            if missing_checks and review.recommended_action == EvidenceAction.SUFFICIENT:
                # A broad Q-POLICY label cannot conceal a missing required parameter citation.
                gaps = [
                    f"检索 {item['source_document_id']} 的 {item['rule_id']} 原文参数 {item['parameters']}"
                    for item in missing_checks
                ]
                review = EvidenceReview(
                    recommended_action=EvidenceAction.RETRIEVE_MORE,
                    reason="必核规则缺少对应额度或提交时限条款，需定向补检；目录参数不能替代原文。",
                    coverage=[
                        item.model_copy(update={"status": CoverageStatus.MISSING})
                        if item.question_id == "Q-POLICY"
                        else item
                        for item in review.coverage
                    ],
                    evidence_gaps=[*review.evidence_gaps, *gaps],
                )
            if review.recommended_action == EvidenceAction.REQUEST_DOCUMENTS:
                exception_ids = [
                    alias
                    for alias, item in evidence_by_alias.items()
                    if item.source_type == EvidenceSource.POLICY_DOCUMENT
                    and "会议主办方指定酒店" in (item.excerpt or "")
                    and "批准例外" in (item.excerpt or "")
                ]
                if exception_ids:
                    review = review.model_copy(
                        update={
                            "coverage": [
                                item.model_copy(
                                    update={
                                        "status": CoverageStatus.SUPPORTED,
                                        "evidence_ids": exception_ids[:1],
                                    }
                                )
                                if item.question_id == "Q-EXCEPTION"
                                and item.status == CoverageStatus.MISSING
                                else item
                                for item in review.coverage
                            ]
                        }
                    )
            review = review.model_copy(
                update={
                    "coverage": [
                        item.model_copy(
                            update={
                                "evidence_ids": [alias_to_id[alias] for alias in item.evidence_ids]
                            }
                        )
                        for item in review.coverage
                    ],
                    "conflict_evidence_ids": [
                        alias_to_id[alias] for alias in review.conflict_evidence_ids
                    ],
                }
            )
            if (
                review.recommended_action == EvidenceAction.RETRIEVE_MORE
                and review.conflict_evidence_ids
                and state["retrieval_round_count"] >= 2
            ):
                review = EvidenceReview(
                    recommended_action=EvidenceAction.ESCALATE,
                    reason=review.reason,
                    coverage=review.coverage,
                    evidence_gaps=review.evidence_gaps,
                    conflict_evidence_ids=review.conflict_evidence_ids,
                )
            return review, total_tokens, total_latency_ms, attempt
        except ValueError:
            if attempt == REVIEWER_FORMAT_ATTEMPTS - 1:
                raise
            payload["repair_feedback"] = {
                "instruction": "修正上一份输出；不得增加问题或证据 ID，并覆盖全部问题。",
                "required_question_ids": sorted(question_ids),
                "allowed_evidence_ids": sorted(valid_ids),
            }
    raise AssertionError("unreachable reviewer retry state")
