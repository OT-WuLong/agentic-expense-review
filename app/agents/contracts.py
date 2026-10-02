"""Validated, public Agent decisions and read-only tool messages."""

from enum import StrEnum
from typing import Literal

from pydantic import AliasChoices, Field, ValidationInfo, model_validator

from app.models import EvidenceItem, StrictModel


# Agent 可调用的只读工具白名单，禁止白名单外的工具
class ToolName(StrEnum):
    POLICY_SEARCH = "policy_search"
    CASE_SEARCH = "case_search"
    RULE_SEARCH = "rule_search"
    STRUCTURED_LOOKUP = "structured_lookup"
    FETCH_DOCUMENT_CONTEXT = "fetch_document_context"


# Supervisor 的阶段级动作：检索、校验、补材料、升级、结束
class SupervisorAction(StrEnum):
    RETRIEVE = "RETRIEVE"
    CHALLENGE_AND_RETRIEVE = "CHALLENGE_AND_RETRIEVE"
    VALIDATE = "VALIDATE"
    REQUEST_DOCUMENTS = "REQUEST_DOCUMENTS"
    ESCALATE = "ESCALATE"
    FINISH = "FINISH"


# Retrieval Agent 的动作：首次检索、观察到缺口后补检、停止
class RetrievalAction(StrEnum):
    RETRIEVE = "RETRIEVE"
    RETRIEVE_MORE = "RETRIEVE_MORE"
    STOP = "STOP"


# Evidence Reviewer 的结论：证据充分、补检、补材料、转人工
class EvidenceAction(StrEnum):
    SUFFICIENT = "SUFFICIENT"
    RETRIEVE_MORE = "RETRIEVE_MORE"
    REQUEST_DOCUMENTS = "REQUEST_DOCUMENTS"
    ESCALATE = "ESCALATE"


# 三个 Agent 角色，用于区分轨迹归属与权限边界
class AgentRole(StrEnum):
    SUPERVISOR = "SUPERVISOR"
    RETRIEVAL = "RETRIEVAL"
    EVIDENCE_REVIEWER = "EVIDENCE_REVIEWER"


# 全部 Agent 动作的并集，供 AgentStep 统一记录
class AgentAction(StrEnum):
    RETRIEVE = "RETRIEVE"
    CHALLENGE_AND_RETRIEVE = "CHALLENGE_AND_RETRIEVE"
    VALIDATE = "VALIDATE"
    REQUEST_DOCUMENTS = "REQUEST_DOCUMENTS"
    ESCALATE = "ESCALATE"
    FINISH = "FINISH"
    RETRIEVE_MORE = "RETRIEVE_MORE"
    STOP = "STOP"
    SUFFICIENT = "SUFFICIENT"


# Supervisor 的结构化决策：动作加简短理由，检索路由必须给出目标
class SupervisorDecision(StrictModel):
    action: SupervisorAction
    reason: str = Field(min_length=1, max_length=500)
    retrieval_goal: str | None = None
    stop_reason: str | None = None
    referenced_call_ids: list[str] = Field(default_factory=list)
    challenged_question_ids: list[str] = Field(default_factory=list)

    # 校验检索路由：动作是 RETRIEVE 时必须给出检索目标
    @model_validator(mode="after")
    def require_retrieval_goal(self) -> "SupervisorDecision":
        retrieval_actions = {
            SupervisorAction.RETRIEVE,
            SupervisorAction.CHALLENGE_AND_RETRIEVE,
        }
        if self.action in retrieval_actions and not self.retrieval_goal:
            raise ValueError("retrieval routing requires a goal")
        if self.action == SupervisorAction.CHALLENGE_AND_RETRIEVE and not self.challenged_question_ids:
            raise ValueError("a challenge must identify at least one question")
        if len(self.challenged_question_ids) > 2:
            raise ValueError("one challenge can target at most two questions")
        if self.action not in retrieval_actions and not self.stop_reason:
            raise ValueError("non-retrieval routing requires a stop reason")
        if len(self.referenced_call_ids) != len(set(self.referenced_call_ids)):
            raise ValueError("referenced call IDs must be unique")
        if len(self.challenged_question_ids) != len(set(self.challenged_question_ids)):
            raise ValueError("challenged question IDs must be unique")
        return self


class SupervisorDecisionRecord(StrictModel):
    sequence: int = Field(ge=1, strict=True)
    decision_source: Literal["LLM", "DETERMINISTIC"]
    trigger: str = Field(min_length=1)
    action: SupervisorAction
    reason: str = Field(min_length=1, max_length=500)
    previous_goal: str | None = None
    new_goal: str | None = None
    stop_reason: str | None = None
    used_model: bool
    referenced_call_ids: list[str] = Field(default_factory=list)
    challenged_question_ids: list[str] = Field(default_factory=list)
    latency_ms: int = Field(ge=0, strict=True)
    tokens_used: int = Field(ge=0, strict=True)


class GuardrailDecision(StrictModel):
    sequence: int = Field(ge=1, strict=True)
    phase: Literal["PRE_TOOL", "POST_REVIEW"]
    action: Literal["VETO", "STOP"]
    reason: str = Field(min_length=1)
    subject_role: AgentRole
    subject_action: AgentAction
    related_supervisor_sequence: int | None = Field(default=None, ge=1, strict=True)


# Retrieval Agent 拆解出的可独立验证子问题
class SubQuestion(StrictModel):
    question_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=500)


# 一次只读工具调用：工具名、查询、用途与受限参数
class ToolCall(StrictModel):
    tool_name: ToolName
    query: str = Field(min_length=1, max_length=500)
    purpose: str = Field(min_length=1, max_length=200)
    query_type: str | None = None
    filters: dict[str, str | int | bool | None] = Field(default_factory=dict)
    arguments: dict[str, str | int | bool | None] = Field(default_factory=dict)
    sub_question_id: str | None = None
    call_id: str | None = None

    # 校验工具参数：禁止 Agent 覆盖服务端授权范围或传入命令参数，结构化查询必须给 query_type
    @model_validator(mode="after")
    def no_agent_owned_scope_or_commands(self) -> "ToolCall":
        forbidden = {
            "authorization_scope",
            "allowed_document_ids",
            "allowed_department_ids",
            "allowed_cities",
            "allowed_tools",
            "allowed_structured_query_types",
            "catalog_snapshot_id",
            "company_context",
            "company_id",
            "document_id",
            "document_ids",
            "policy_catalog_snapshot_id",
            "policy_source_snapshot_ids",
            "role",
            "requester_role",
            "structured_as_of",
            "structured_data_snapshot_id",
            "structured_data_snapshot_ids",
            "structured_source_snapshot_ids",
            "tenant_id",
            "department_id",
            "department_ids",
            "sql",
            "url",
            "shell",
            "command",
        }
        if forbidden & {key.casefold() for key in self.filters | self.arguments}:
            raise ValueError("Agent cannot set server authorization scope or command parameters")
        if self.tool_name == ToolName.STRUCTURED_LOOKUP and not self.query_type:
            raise ValueError("structured lookup requires a query type")
        return self


# Retrieval Agent 的检索计划：子问题与工具调用的组合，或终止
class RetrievalPlan(StrictModel):
    action: RetrievalAction
    reason: str = Field(min_length=1, max_length=500)
    sub_questions: list[SubQuestion] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    is_query_rewrite: bool = False

    @model_validator(mode="before")
    @classmethod
    def restore_server_questions(cls, data: object, info: ValidationInfo) -> object:
        # The checklist belongs to the server. An omitted echo is not a new search plan.
        required = (info.context or {}).get("required_questions", [])
        if (
            isinstance(data, dict)
            and data.get("action") != RetrievalAction.STOP
            and not data.get("sub_questions")
            and required
        ):
            return {**data, "sub_questions": required}
        return data

    # 校验检索计划：STOP 不携带工具，非 STOP 必须有子问题和工具，且引用一致
    @model_validator(mode="after")
    def check_plan(self) -> "RetrievalPlan":
        if self.action == RetrievalAction.STOP:
            if self.tool_calls or self.is_query_rewrite:
                raise ValueError("stopping cannot request tools or a query rewrite")
        elif not self.sub_questions or not self.tool_calls:
            raise ValueError("retrieval requires a sub-question and a read-only tool call")
        question_ids = {item.question_id for item in self.sub_questions}
        if len(question_ids) != len(self.sub_questions):
            raise ValueError("sub-question IDs must be unique")
        if any(
            call.sub_question_id is not None and call.sub_question_id not in question_ids
            for call in self.tool_calls
        ):
            raise ValueError("tool call references an unknown sub-question")
        if self.is_query_rewrite and self.action != RetrievalAction.RETRIEVE_MORE:
            raise ValueError("query rewrite must be an observed follow-up action")
        return self


# 工具统一返回值：候选证据、来源、耗时与降级/错误状态
class ToolObservation(StrictModel):
    tool_name: ToolName
    query: str = Field(min_length=1)
    items: list[EvidenceItem] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    latency_ms: int = Field(ge=0, strict=True)
    error: str | None = None
    is_degraded: bool = False
    call_id: str | None = None
    call_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    new_evidence_ids: list[str] = Field(default_factory=list)

    # 校验工具返回：来源 ID 必须与 EvidenceItem 对齐，新增证据只能来自本次返回
    @model_validator(mode="after")
    def check_evidence_ids(self) -> "ToolObservation":
        expected = [item.evidence_id for item in self.items]
        if self.source_ids != expected:
            raise ValueError("source IDs must match returned EvidenceItem IDs in order")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("tool observations cannot contain duplicate source IDs")
        if not set(self.new_evidence_ids) <= set(self.source_ids):
            raise ValueError("new evidence IDs must come from this observation")
        if len(self.new_evidence_ids) != len(set(self.new_evidence_ids)):
            raise ValueError("new evidence IDs must be unique")
        return self


# 子问题的证据覆盖状态：有支持、缺失、冲突
class CoverageStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    MISSING = "MISSING"
    CONFLICTING = "CONFLICTING"


# 单个子问题的证据覆盖情况，必须携带证据引用
class QuestionCoverage(StrictModel):
    question_id: str = Field(min_length=1)
    status: CoverageStatus
    evidence_ids: list[str] = Field(default_factory=list)

    # 校验覆盖引用：SUPPORTED 必须有证据，CONFLICTING 至少两条证据
    @model_validator(mode="after")
    def require_citations(self) -> "QuestionCoverage":
        if self.status == CoverageStatus.SUPPORTED and not self.evidence_ids:
            raise ValueError("supported coverage requires an Evidence ID")
        if self.status == CoverageStatus.CONFLICTING and len(set(self.evidence_ids)) < 2:
            raise ValueError("conflict requires at least two Evidence IDs")
        return self


# Evidence Reviewer 的评审结论：覆盖、缺口、冲突与下一步动作
class EvidenceReview(StrictModel):
    recommended_action: EvidenceAction = Field(
        validation_alias=AliasChoices("recommended_action", "action")
    )
    reason: str = Field(min_length=1, max_length=500)
    coverage: list[QuestionCoverage] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    conflict_evidence_ids: list[str] = Field(default_factory=list)

    # 校验充分性：判为 SUFFICIENT 时不得残留缺失、冲突或证据缺口
    @model_validator(mode="after")
    def check_sufficiency(self) -> "EvidenceReview":
        if self.recommended_action == EvidenceAction.SUFFICIENT and (
            not self.coverage
            or any(item.status != CoverageStatus.SUPPORTED for item in self.coverage)
            or self.evidence_gaps
            or self.conflict_evidence_ids
        ):
            raise ValueError("sufficient review cannot contain missing or conflicting evidence")
        return self

    @property
    def action(self) -> EvidenceAction:
        """Compatibility accessor; routing authority belongs to Supervisor."""

        return self.recommended_action


# 一步 Agent 轨迹：角色、动作、简短理由、工具调用与证据增量
class AgentStep(StrictModel):
    step_index: int = Field(ge=1, strict=True)
    agent: AgentRole
    action: AgentAction
    thought_summary: str = Field(min_length=1, max_length=500)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)
    new_evidence_ids: list[str] = Field(default_factory=list)
    latency_ms: int = Field(ge=0, strict=True)
    tokens_used: int = Field(default=0, ge=0, strict=True)
    stop_reason: str | None = None

    # 校验角色边界：动作必须属于该角色，只有 Retrieval 能规划工具调用
    @model_validator(mode="after")
    def keep_agent_roles_separate(self) -> "AgentStep":
        allowed = {
            AgentRole.SUPERVISOR: {action.value for action in SupervisorAction},
            AgentRole.RETRIEVAL: {action.value for action in RetrievalAction},
            AgentRole.EVIDENCE_REVIEWER: {action.value for action in EvidenceAction},
        }
        if self.action.value not in allowed[self.agent]:
            raise ValueError("action does not belong to this Agent role")
        if self.agent != AgentRole.RETRIEVAL and self.tool_calls:
            raise ValueError("only Retrieval Agent can plan tool calls")
        if self.agent == AgentRole.RETRIEVAL:
            if self.action == AgentAction.STOP and self.tool_calls:
                raise ValueError("stopping cannot plan tool calls")
            if self.action != AgentAction.STOP and not self.tool_calls:
                raise ValueError("retrieval step must record its planned tool calls")
        return self
