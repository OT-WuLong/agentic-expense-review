"""Business contracts shared by the API, rules, and retrieval workflow."""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


# 金额解析：只接受 Decimal、整数或十进制字符串，拒绝 float/bool，并保证有限值
def parse_money(value: object) -> Decimal:
    # Pydantic converts validator ValueError, but lets TypeError escape unwrapped.
    if isinstance(value, (bool, float)):
        raise ValueError("money must not come from a boolean or binary float")  # noqa: TRY004
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, str):
        try:
            amount = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError("invalid decimal amount") from exc
    else:
        raise ValueError("money must be a decimal, integer, or decimal string")  # noqa: TRY004
    if not amount.is_finite():
        raise ValueError("money must be finite")
    return amount


# 日期解析：只接受 YYYY-MM-DD 格式，拒绝带时间的 datetime
def parse_date(value: object) -> date:
    if isinstance(value, datetime):
        raise ValueError("date must not contain a time")  # noqa: TRY004
    if isinstance(value, date):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("invalid calendar date") from exc
    raise ValueError("date must use YYYY-MM-DD")


Money = Annotated[Decimal, BeforeValidator(parse_money)]
DateOnly = Annotated[date, BeforeValidator(parse_date)]


# 费用类型枚举：v1 固定支持的交通、住宿、餐饮三类
class ExpenseType(StrEnum):
    TRANSPORT = "交通"
    LODGING = "住宿"
    DINING = "餐饮"


# 审批状态枚举：CREATED/RUNNING 为运行生命周期，COMPLETED/HUMAN_PENDING 为终态，其余为终态结果，
# 并且必须与 recommendation 保持合法组合，避免技术故障或半成品被误报成业务结论
class ApprovalStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    BUSINESS_REJECTED = "BUSINESS_REJECTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    HUMAN_PENDING = "HUMAN_PENDING"
    COMPLETED = "COMPLETED"


# 风险等级：由确定性风险特征计算，不使用 LLM 自报置信度
class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# 单条规则校验的结果：通过 / 不通过 / 无法判定
class RuleOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INDETERMINATE = "INDETERMINATE"


# 三类预审建议：建议通过 / 建议驳回 / 转人工复核
class Recommendation(StrEnum):
    PASS_RECOMMENDED = "PASS_RECOMMENDED"
    REJECT_RECOMMENDED = "REJECT_RECOMMENDED"
    HUMAN_REVIEW = "HUMAN_REVIEW"


# 证据来源类型：制度文档、相邻文档上下文、附件、结构化记录
class EvidenceSource(StrEnum):
    POLICY_DOCUMENT = "POLICY_DOCUMENT"
    DOCUMENT_CONTEXT = "DOCUMENT_CONTEXT"
    ATTACHMENT = "ATTACHMENT"
    STRUCTURED_RECORD = "STRUCTURED_RECORD"
    CASE_MEMORY = "CASE_MEMORY"
    RULE_CATALOG = "RULE_CATALOG"


# 字段抽取状态：正常抽取 / 缺失 / 不可读
class ExtractionStatus(StrEnum):
    PRESENT = "PRESENT"
    MISSING = "MISSING"
    UNREADABLE = "UNREADABLE"


# 所有业务模型的基类：禁止未声明字段，防止节点越权写入或拼错字段
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# 申请人信息：员工、部门与显示名
class Applicant(StrictModel):
    employee_id: str = Field(min_length=1)
    department_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)


# 报销申请主体：费用类型、金额、日期，以及住宿/交通等可选字段
class Application(StrictModel):
    expense_type: ExpenseType
    currency: str
    amount: Money = Field(gt=0)
    occurred_on: DateOnly
    submitted_on: DateOnly
    description: str = Field(min_length=1, max_length=2000)
    transport_purpose: str | None = None
    origin_type: str | None = None
    destination_type: str | None = None
    city: str | None = None
    check_in: DateOnly | None = None
    check_out: DateOnly | None = None
    room_count: int | None = Field(default=None, ge=1, strict=True)
    attendee_count: int | None = Field(default=None, ge=1, strict=True)

    # 归一化币种：去除空白并转大写，必须是三位字母代码
    @model_validator(mode="before")
    @classmethod
    def normalize_currency(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        currency = data.get("currency")
        if not isinstance(currency, str):
            raise ValueError("currency must be a three-letter code")  # noqa: TRY004
        normalized = currency.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", normalized):
            raise ValueError("currency must be a three-letter code")
        return {**data, "currency": normalized}

    # 校验日期先后：提交日不能早于发生日，退房日必须晚于入住日
    @model_validator(mode="after")
    def check_dates(self) -> "Application":
        if self.submitted_on < self.occurred_on:
            raise ValueError("submission cannot predate the expense")
        if self.check_in and self.check_out and self.check_out <= self.check_in:
            raise ValueError("check-out must be after check-in")
        return self


# 附件元数据：文档类型、媒体类型，以及是否为合成数据
class Document(StrictModel):
    document_id: str = Field(min_length=1)
    document_type: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    fixture_key: str | None = None
    synthetic: bool = False


# 一次预审请求的完整输入：请求 ID、申请人、申请与附件列表
class ApprovalInput(StrictModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    applicant: Applicant
    application: Application
    documents: list[Document] = Field(default_factory=list)


# 从附件抽取的单个字段：值、原始值、来源文档与页码
class ExtractedField(StrictModel):
    field: str = Field(min_length=1)
    status: ExtractionStatus
    value: str | int | bool | None = None
    raw_value: str | None = None
    document_id: str | None = None
    page: int | None = Field(default=None, ge=1, strict=True)

    # 校验抽取来源：PRESENT 必须有值和文档页码，其余状态不能带值
    @model_validator(mode="after")
    def check_source(self) -> "ExtractedField":
        if self.status == ExtractionStatus.PRESENT and (
            self.value is None or not self.document_id or self.page is None
        ):
            raise ValueError("present field requires value, document ID, and page")
        if self.status != ExtractionStatus.PRESENT and self.value is not None:
            raise ValueError("missing or unreadable field cannot have a value")
        return self


# 一条可定位的证据：文档/版本/页码/片段，或结构化记录快照
class EvidenceItem(StrictModel):
    evidence_id: str = Field(min_length=1)
    source_type: EvidenceSource
    score: float | None = Field(default=None, allow_inf_nan=False)
    document_id: str | None = None
    version: str | None = None
    effective_from: DateOnly | None = None
    effective_to: DateOnly | None = None
    page: int | None = Field(default=None, ge=1, strict=True)
    section: str | None = None
    excerpt: str | None = None
    catalog_snapshot_id: str | None = None
    published_status: str | None = None
    authority_level: str | None = None
    priority: int | None = None
    supersedes_document_id: str | None = None
    structured_data_snapshot_id: str | None = None
    fixture_id: str | None = None
    query_type: str | None = None
    record_key: str | None = None
    snapshot_version: str | None = None
    effective_at: DateOnly | None = None
    value: str | int | bool | None = None
    available_amount: Money | None = None
    consumer: str | None = None
    case_id: str | None = None
    rule_id: str | None = None

    # 校验证据来源：结构化证据不得伪造页码，文档证据须可定位，制度正文须有生效日期
    @model_validator(mode="after")
    def check_provenance(self) -> "EvidenceItem":
        if self.source_type == EvidenceSource.STRUCTURED_RECORD:
            if not all(
                (
                    self.structured_data_snapshot_id,
                    self.query_type,
                    self.record_key,
                    self.snapshot_version,
                )
            ):
                raise ValueError("structured evidence requires query, record, and snapshot source")
            if self.document_id is not None or self.page is not None:
                raise ValueError("structured evidence must not fabricate document source or page")
        elif self.source_type == EvidenceSource.CASE_MEMORY:
            if not self.case_id or not self.excerpt or self.document_id or self.page:
                raise ValueError("case memory requires an anonymized case ID and summary")
        elif self.source_type == EvidenceSource.RULE_CATALOG:
            if not self.rule_id or not self.version or not self.excerpt:
                raise ValueError("rule catalog evidence requires a rule ID, version, and summary")
            if self.page is not None:
                raise ValueError("rule catalog evidence cannot fabricate a source page")
        else:
            if not all((self.document_id, self.version, self.page, self.excerpt)):
                raise ValueError("document evidence requires ID, version, page, and excerpt")
            if (
                self.source_type == EvidenceSource.POLICY_DOCUMENT
                and self.effective_from is None
            ):
                raise ValueError("policy evidence requires an effective start date")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective end cannot predate start")
        return self


# 单条硬规则的执行结果：规则 ID、版本、结论与依据引用
class RuleResult(StrictModel):
    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    producer: Literal["RULE_VALIDATOR"]
    outcome: RuleOutcome
    input_refs: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    reason_code: str | None = None
    computed_limit: Money | None = None
    actual_amount: Money | None = None
    computed_value: str | int | Decimal | None = None
    unit: str | None = None


# 结构化的决策原因：原因码、说明，以及引用的证据与规则
class DecisionReason(StrictModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)


# 风险特征指标：由确定性代码计算，未测量时为 None，不使用模型自报置信度
class EvidenceMetrics(StrictModel):
    evidence_coverage: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    citation_completeness: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    field_consistency: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    retrieval_margin: float | None = Field(default=None, allow_inf_nan=False)
    rule_conflict_count: int | None = Field(default=None, ge=0, strict=True)
    extraction_quality: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


# 待人工处理的动作类型：请求补充材料，或转入人工复核
class HumanActionType(StrEnum):
    REQUEST_DOCUMENTS = "REQUEST_DOCUMENTS"
    REVIEW = "REVIEW"


class HumanReviewAction(StrEnum):
    APPROVE = "APPROVE"
    EDIT = "EDIT"
    REJECT = "REJECT"


# 挂起等待人工处理的请求：动作、原因与缺失材料清单
class HumanAction(StrictModel):
    action: HumanActionType
    reason: str = Field(min_length=1, max_length=500)
    missing_items: list[str] = Field(default_factory=list)


# 人工终审决定：操作者、最终建议与理由，并保留被修改前的预审建议以便审计差异
class FinalDecision(StrictModel):
    action: HumanReviewAction
    operator_id: str = Field(min_length=1)
    recommendation: Recommendation
    reason: str = Field(min_length=1, max_length=500)
    previous_recommendation: Recommendation | None = None


class HumanReviewPayload(StrictModel):
    request_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    recommendation: Recommendation | None = None
    risk_level: RiskLevel | None = None
    reasons: list[DecisionReason] = Field(default_factory=list)
    rule_results: list[RuleResult] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    missing_items: list[str] = Field(default_factory=list)
    allowed_actions: list[HumanReviewAction] = Field(min_length=1)


class HumanReviewSubmission(StrictModel):
    action: HumanReviewAction
    operator_id: str = Field(min_length=1)
    reviewer_role: Literal["FINANCE_REVIEWER"]
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=128)
    recommendation: Recommendation | None = None

    @model_validator(mode="after")
    def validate_action(self) -> "HumanReviewSubmission":
        if self.action == HumanReviewAction.EDIT:
            if self.recommendation not in {
                Recommendation.PASS_RECOMMENDED,
                Recommendation.REJECT_RECOMMENDED,
            }:
                raise ValueError("edit requires a pass or reject recommendation")
        elif self.recommendation is not None:
            raise ValueError("approve and reject derive their recommendation from the action")
        return self


# 状态与业务建议的合法组合：未形成建议时只能停留在运行态或等待人工，不能假装已有业务结论
_STATUSES_BY_RECOMMENDATION: dict[Recommendation | None, set[ApprovalStatus]] = {
    None: {
        ApprovalStatus.CREATED,
        ApprovalStatus.RUNNING,
        ApprovalStatus.HUMAN_PENDING,
        ApprovalStatus.INSUFFICIENT_EVIDENCE,
        ApprovalStatus.SYSTEM_ERROR,
    },
    Recommendation.PASS_RECOMMENDED: {ApprovalStatus.COMPLETED},
    Recommendation.REJECT_RECOMMENDED: {
        ApprovalStatus.BUSINESS_REJECTED,
        ApprovalStatus.COMPLETED,
    },
    Recommendation.HUMAN_REVIEW: {
        ApprovalStatus.HUMAN_PENDING,
        ApprovalStatus.INSUFFICIENT_EVIDENCE,
    },
}


# 对外返回的预审结果：状态、建议、风险、原因、规则与证据
class ApprovalResponse(StrictModel):
    request_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    status: ApprovalStatus
    recommendation: Recommendation | None = None
    risk_level: RiskLevel | None = None
    reasons: list[DecisionReason] = Field(default_factory=list)
    rule_results: list[RuleResult] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    final_decision: FinalDecision | None = None

    # 校验结果一致性：系统错误不能带业务建议，自动建议必须有原因、证据与命中规则
    @model_validator(mode="after")
    def separate_system_failure(self) -> "ApprovalResponse":
        if self.status == ApprovalStatus.SYSTEM_ERROR and self.recommendation is not None:
            raise ValueError("system errors cannot be business recommendations")
        if self.status not in _STATUSES_BY_RECOMMENDATION[self.recommendation]:
            raise ValueError("status is inconsistent with the recommendation")
        if self.final_decision is not None and (
            self.status != ApprovalStatus.COMPLETED
            or self.final_decision.recommendation != self.recommendation
        ):
            raise ValueError("human final decision must match a completed response")
        if self.recommendation is not None and not self.reasons:
            raise ValueError("business recommendations require structured reasons")
        evidence_ids = {item.evidence_id for item in self.evidence}
        rule_ids = {item.rule_id for item in self.rule_results}
        for reason in self.reasons:
            if not set(reason.evidence_ids) <= evidence_ids or not set(reason.rule_ids) <= rule_ids:
                raise ValueError("reason references must exist in the response")
        automatic = self.final_decision is None
        if automatic and self.recommendation in {
            Recommendation.PASS_RECOMMENDED,
            Recommendation.REJECT_RECOMMENDED,
        }:
            if not self.evidence or not self.rule_results:
                raise ValueError("automatic recommendations require evidence and rule results")
            if not any(reason.evidence_ids for reason in self.reasons) or not any(
                reason.rule_ids for reason in self.reasons
            ):
                raise ValueError("automatic recommendation reasons must cite evidence and rules")
        if automatic and self.recommendation == Recommendation.PASS_RECOMMENDED and any(
            result.outcome != RuleOutcome.PASS for result in self.rule_results
        ):
            raise ValueError("pass recommendation cannot include a failed or indeterminate rule")
        if automatic and self.recommendation == Recommendation.REJECT_RECOMMENDED:
            failed_rule_ids = {
                result.rule_id for result in self.rule_results if result.outcome == RuleOutcome.FAIL
            }
            if not failed_rule_ids or not any(
                failed_rule_ids & set(reason.rule_ids) for reason in self.reasons
            ):
                raise ValueError("reject recommendation must cite a failed rule")
        return self
