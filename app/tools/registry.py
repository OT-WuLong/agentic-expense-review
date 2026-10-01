"""Explicit, fail-closed registry for the small v1 read-only tool set."""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator
from requests import Timeout as RequestsTimeout

from app.agents.contracts import ToolCall, ToolName, ToolObservation
from app.models import DateOnly, ExpenseType, StrictModel

COMPANY_CONTEXT = "SINGLE_COMPANY_V1"
_WHITESPACE = re.compile(r"\s+")


# 服务端注入的授权与应用事实上下文：Agent 只能读取，不能自行构造
class ToolExecutionContext(StrictModel):
    """Authorization and application facts injected by the server, never the Agent."""

    company_context: Literal["SINGLE_COMPANY_V1"] = COMPANY_CONTEXT
    request_id: str | None = None
    requester_role: str = Field(min_length=1)
    department_id: str = Field(min_length=1)
    allowed_department_ids: list[str] = Field(min_length=1)
    allowed_document_ids: list[str] = Field(default_factory=list)
    applicable_rule_ids: list[str] = Field(default_factory=list)
    rule_scope_complete: bool = False
    allowed_tools: list[ToolName] = Field(min_length=1)
    allowed_structured_query_types: list[
        Literal["city_tier", "employee_department", "budget_status", "duplicate_invoice"]
    ] = Field(default_factory=list)
    policy_catalog_snapshot_id: str = Field(min_length=1)
    structured_data_snapshot_id: str = Field(min_length=1)
    policy_source_snapshot_ids: list[str] = Field(default_factory=list)
    structured_source_snapshot_ids: list[str] = Field(default_factory=list)
    policy_effective_at: DateOnly
    structured_as_of: DateOnly
    expense_type: ExpenseType
    allowed_cities: list[str] = Field(default_factory=list)
    allowed_employee_ids: list[str] = Field(default_factory=list)
    allowed_invoice_numbers: list[str] = Field(default_factory=list)

    @property
    def policy_snapshots(self) -> list[str]:
        return self.policy_source_snapshot_ids or [self.policy_catalog_snapshot_id]

    @property
    def structured_snapshots(self) -> list[str]:
        return self.structured_source_snapshot_ids or [self.structured_data_snapshot_id]

    # 校验授权范围：各白名单必须去重、请求部门必须在授权部门内、结构化查询时间不得早于制度生效日
    @model_validator(mode="after")
    def validate_scope(self) -> ToolExecutionContext:
        collections = (
            self.allowed_department_ids,
            self.allowed_document_ids,
            self.applicable_rule_ids,
            self.allowed_tools,
            self.allowed_structured_query_types,
            self.allowed_cities,
            self.allowed_employee_ids,
            self.allowed_invoice_numbers,
            self.policy_source_snapshot_ids,
            self.structured_source_snapshot_ids,
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError("authorization scope values must be unique")
        if self.department_id not in self.allowed_department_ids:
            raise ValueError("request department must be inside the server authorization scope")
        if self.structured_as_of < self.policy_effective_at:
            raise ValueError("structured lookup time cannot predate the policy effective date")
        return self


# 工具处理器签名：调用、服务端上下文、结果上限、超时预算 → 受控观察结果
ToolHandler = Callable[[ToolCall, ToolExecutionContext, int, float], ToolObservation]


# 单个工具的注册规格：名称、处理器、超时预算与结果上限
@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: ToolName
    handler: ToolHandler
    timeout_seconds: float
    max_results: int

    # 校验超时预算与结果上限必须为正数
    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.max_results <= 0:
            raise ValueError("tool timeout and result limit must be positive")


# 请求了未注册或未知工具名时抛出的错误
class UnknownToolError(LookupError):
    pass


# 递归规范化任意值：文本做 NFC 与空白折叠，日期与金额转字符串，映射按键排序
def _canonical(value: object) -> object:
    if isinstance(value, str):
        return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", value).strip())
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


# 计算幂等调用哈希：工具名 + 完整授权范围 + 有效参数，用于缓存与审计
def tool_call_hash(call: ToolCall, context: ToolExecutionContext) -> str:
    payload = {
        "tool_name": call.tool_name.value,
        "company_context": context.company_context,
        "requester_role": context.requester_role,
        "department_id": context.department_id,
        "allowed_department_ids": sorted(context.allowed_department_ids),
        "allowed_document_ids": sorted(context.allowed_document_ids),
        "allowed_tools": sorted(item.value for item in context.allowed_tools),
        "allowed_structured_query_types": sorted(context.allowed_structured_query_types),
        "policy_catalog_snapshot_id": context.policy_catalog_snapshot_id,
        "structured_data_snapshot_id": context.structured_data_snapshot_id,
        "policy_effective_at": context.policy_effective_at,
        "structured_as_of": context.structured_as_of,
        "expense_type": context.expense_type.value,
        "allowed_cities": sorted(context.allowed_cities),
        "allowed_employee_ids": sorted(context.allowed_employee_ids),
        "allowed_invoice_numbers": sorted(context.allowed_invoice_numbers),
        "parameters": {
            "query": call.query if call.tool_name != ToolName.STRUCTURED_LOOKUP else None,
            "query_type": call.query_type,
            "filters": call.filters,
            "arguments": call.arguments,
        },
    }
    # Keep hashes for single-source historical checkpoints unchanged.
    if len(context.policy_snapshots) > 1:
        payload["policy_source_snapshot_ids"] = sorted(context.policy_snapshots)
    if len(context.structured_snapshots) > 1:
        payload["structured_source_snapshot_ids"] = sorted(context.structured_snapshots)
    encoded = json.dumps(
        _canonical(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# 只读工具注册表：按白名单注册工具，并以失败封闭的方式执行调用
class ToolRegistry:
    # 初始化注册表，注册重复工具名时直接报错
    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        self._specs: dict[ToolName, ToolSpec] = {}
        for spec in specs:
            if spec.name in self._specs:
                raise ValueError(f"duplicate tool registration: {spec.name}")
            self._specs[spec.name] = spec

    # 按枚举或字符串查找工具规格，未知工具统一抛 UnknownToolError
    def get(self, name: ToolName | str) -> ToolSpec:
        try:
            tool_name = name if isinstance(name, ToolName) else ToolName(name)
            return self._specs[tool_name]
        except (KeyError, ValueError) as exc:
            raise UnknownToolError(f"unknown or unregistered tool: {name}") from exc

    # 执行工具调用：未授权、超时、权限不足与异常都转为受控观察结果，并截断结果、计算新增证据 ID
    def execute(
        self,
        call: ToolCall,
        context: ToolExecutionContext,
        *,
        known_evidence_ids: Iterable[str] = (),
    ) -> ToolObservation:
        call_hash = tool_call_hash(call, context)
        started = time.perf_counter()
        if call.tool_name not in context.allowed_tools:
            return ToolObservation(
                tool_name=call.tool_name,
                query=call.query,
                latency_ms=0,
                error="TOOL_NOT_AUTHORIZED",
                call_id=call.call_id,
                call_hash=call_hash,
            )
        try:
            spec = self.get(call.tool_name)
        except UnknownToolError:
            return ToolObservation(
                tool_name=call.tool_name,
                query=call.query,
                latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
                error="TOOL_NOT_IMPLEMENTED",
                is_degraded=True,
                call_id=call.call_id,
                call_hash=call_hash,
            )
        try:
            observation = spec.handler(
                call, context, spec.max_results, spec.timeout_seconds
            )
        except (TimeoutError, RequestsTimeout):
            observation = ToolObservation(
                tool_name=call.tool_name,
                query=call.query,
                latency_ms=0,
                error="TOOL_TIMEOUT",
                is_degraded=True,
                call_id=call.call_id,
            )
        except PermissionError:
            observation = ToolObservation(
                tool_name=call.tool_name,
                query=call.query,
                latency_ms=0,
                error="AUTHORIZATION_DENIED",
                call_id=call.call_id,
            )
        except Exception:  # noqa: BLE001 - tool boundary must not leak dependency errors
            observation = ToolObservation(
                tool_name=call.tool_name,
                query=call.query,
                latency_ms=0,
                error="TOOL_EXECUTION_ERROR",
                is_degraded=True,
                call_id=call.call_id,
            )
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        items = observation.items[: spec.max_results]
        source_ids = [item.evidence_id for item in items]
        known = set(known_evidence_ids)
        return ToolObservation(
            tool_name=call.tool_name,
            query=call.query,
            items=items,
            source_ids=source_ids,
            latency_ms=latency_ms,
            error=observation.error,
            is_degraded=observation.is_degraded,
            call_id=call.call_id,
            call_hash=call_hash,
            new_evidence_ids=[item for item in source_ids if item not in known],
        )
