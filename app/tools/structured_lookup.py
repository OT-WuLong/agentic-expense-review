"""Schema-bound structured lookup backed by an injected record finder."""

from __future__ import annotations

from typing import Any

from pydantic import Field, ValidationError

from app.agents.contracts import ToolCall, ToolName, ToolObservation
from app.models import StrictModel
from app.structured import StructuredRecordFinder, structured_record_to_evidence
from app.tools.registry import ToolExecutionContext


# city_tier 入参：城市名
class CityTierInput(StrictModel):
    city: str = Field(min_length=1)


# employee_department 入参：员工 ID
class EmployeeDepartmentInput(StrictModel):
    employee_id: str = Field(min_length=1)


# budget_status 入参：YYYY-MM 格式的预算期间
class BudgetStatusInput(StrictModel):
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


# duplicate_invoice 入参：票据号
class DuplicateInvoiceInput(StrictModel):
    invoice_number: str = Field(min_length=1)


# 四种结构化查询类型到入参模型的映射
_INPUT_MODELS: dict[str, type[StrictModel]] = {
    "city_tier": CityTierInput,
    "employee_department": EmployeeDepartmentInput,
    "budget_status": BudgetStatusInput,
    "duplicate_invoice": DuplicateInvoiceInput,
}


# 结构化查询工具：查询类型与记录键都受服务端授权范围约束
class StructuredLookupTool:
    # 注入结构化记录仓库
    def __init__(self, store: StructuredRecordFinder) -> None:
        self.store = store

    # 校验查询类型与记录键在授权范围内后查库，查不到返回空结果而非系统错误
    def __call__(
        self,
        call: ToolCall,
        context: ToolExecutionContext,
        max_results: int,
        timeout_seconds: float,
    ) -> ToolObservation:
        del max_results, timeout_seconds
        model = _INPUT_MODELS.get(call.query_type or "")
        if model is None or call.filters:
            return self._error(call, "UNSUPPORTED_QUERY_TYPE")
        if call.query_type not in context.allowed_structured_query_types:
            return self._error(call, "AUTHORIZATION_DENIED")
        try:
            request = model.model_validate(call.arguments)
        except ValidationError:
            return self._error(call, "INVALID_TOOL_INPUT")
        key = self._record_key(call.query_type or "", request, context)
        if key is None:
            return self._error(call, "AUTHORIZATION_DENIED")
        as_of = (
            context.policy_effective_at
            if call.query_type in {"city_tier", "employee_department"}
            else context.structured_as_of
        )
        found = self.store.find(
            query_type=call.query_type or "",
            record_key=key,
            snapshot_ids=set(context.structured_snapshots),
            as_of=as_of,
            request_id=context.request_id,
        )
        if found is None and call.query_type == "city_tier" and not key.endswith("市"):
            found = self.store.find(
                query_type="city_tier",
                record_key=f"{key}市",
                snapshot_ids=set(context.structured_snapshots),
                as_of=as_of,
            )
        if found is None:
            return ToolObservation(
                tool_name=ToolName.STRUCTURED_LOOKUP,
                query=call.query,
                latency_ms=0,
                call_id=call.call_id,
            )
        snapshot_id, record = found
        evidence = structured_record_to_evidence(snapshot_id, record)
        return ToolObservation(
            tool_name=ToolName.STRUCTURED_LOOKUP,
            query=call.query,
            items=[evidence],
            source_ids=[evidence.evidence_id],
            latency_ms=0,
            call_id=call.call_id,
        )

    # 由授权范围推导记录键：城市、员工与票据号必须在白名单内，预算只允许查询当前月份
    @staticmethod
    def _record_key(
        query_type: str, request: StrictModel, context: ToolExecutionContext
    ) -> str | None:
        values: dict[str, Any] = request.model_dump()
        if query_type == "city_tier":
            city = values["city"]
            return city if city in context.allowed_cities else None
        if query_type == "employee_department":
            employee_id = values["employee_id"]
            return employee_id if employee_id in context.allowed_employee_ids else None
        if query_type == "budget_status":
            period = values["period"]
            return (
                f"{context.department_id}:{period}"
                if period == context.structured_as_of.strftime("%Y-%m")
                else None
            )
        if query_type == "duplicate_invoice":
            invoice_number = values["invoice_number"]
            return (
                invoice_number if invoice_number in context.allowed_invoice_numbers else None
            )
        raise AssertionError("validated structured query type is not implemented")

    # 构造统一格式的错误观察结果
    @staticmethod
    def _error(call: ToolCall, code: str) -> ToolObservation:
        return ToolObservation(
            tool_name=ToolName.STRUCTURED_LOOKUP,
            query=call.query,
            latency_ms=0,
            error=code,
            call_id=call.call_id,
        )
