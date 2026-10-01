"""Deterministic P08 expense rules; LLM output is never an input to rule outcomes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.database import PolicyRuleRecord, PostgresStore
from app.graph.state import ApprovalState
from app.models import (
    EvidenceItem,
    EvidenceMetrics,
    EvidenceSource,
    ExpenseType,
    ExtractionStatus,
    RuleOutcome,
    RuleResult,
)
from app.structured import DEMO_STRUCTURED_SNAPSHOT_ID, structured_record_to_evidence
from app.tools.registry import ToolExecutionContext


@dataclass(slots=True)
class RuleEvaluation:
    results: list[RuleResult]
    new_evidence: list[EvidenceItem]
    risk_flags: list[str]
    metrics: EvidenceMetrics


_REQUIRED_RULE_IDS: dict[ExpenseType, tuple[str, ...]] = {
    ExpenseType.TRANSPORT: (
        "RULE-EXPENSE-TYPE-IN-SCOPE",
        "RULE-AMOUNT-DATE-CONSISTENCY",
        "RULE-TRANSPORT-PURPOSE-ELIGIBILITY",
    ),
    ExpenseType.LODGING: (
        "RULE-EXPENSE-TYPE-IN-SCOPE",
        "RULE-LODGING-NIGHT-COUNT",
        "RULE-LODGING-AMOUNT-LIMIT",
        "RULE-LODGING-DOCUMENT-CONSISTENCY",
        "RULE-SUBMISSION-TIMELINESS",
        "RULE-DUPLICATE-INVOICE",
        "RULE-BUDGET-AVAILABLE",
        "RULE-APPLICANT-PERMISSION",
    ),
    ExpenseType.DINING: (
        "RULE-EXPENSE-TYPE-IN-SCOPE",
        "RULE-MEAL-DOCUMENT-CONSISTENCY",
        "RULE-POLICY-VERSION-UNIQUENESS",
        "RULE-MEAL-LIMIT-USING-OLD-POLICY",
        "RULE-MEAL-LIMIT-USING-NEW-POLICY",
        "RULE-MEAL-LIMIT-DETERMINATION",
    ),
}

_DINING_VERSION_DOCUMENTS = {
    "RULE-MEAL-LIMIT-USING-OLD-POLICY": "POL-DINING-2025-V1",
    "RULE-MEAL-LIMIT-USING-NEW-POLICY": "POL-DINING-2026-V2",
}

_SALES_DEPARTMENT = "DEPT-SALES"
_OPS_MKT_DEPARTMENTS = {"DEPT-MARKETING", "DEPT-OPERATIONS"}
_ROUTE_TEXT = re.compile(
    r"(?:路线|行程|起止地点)\s*[:：]\s*[^\n。]{3,120}|"
    r"(?:起点|上车地点)\s*[:：]\s*[^；;，,。\n]+[；;，,]\s*"
    r"(?:终点|下车地点)\s*[:：]\s*[^\n。]{2,120}"
)


def _field(state: ApprovalState, name: str) -> object | None:
    return next(
        (
            item.value
            for item in state.get("extracted_fields", [])
            if item.field == name and item.status == ExtractionStatus.PRESENT
        ),
        None,
    )


def _attachment_ids(state: ApprovalState, *document_types: str) -> list[str]:
    document_ids = {
        item.document_id
        for item in state.get("documents", [])
        if not document_types or item.document_type in document_types
    }
    return [
        item.evidence_id
        for item in state.get("evidence", [])
        if item.source_type == EvidenceSource.ATTACHMENT
        and item.document_id in document_ids
    ]


def _attachment_items(
    state: ApprovalState, *document_types: str
) -> list[EvidenceItem]:
    ids = set(_attachment_ids(state, *document_types))
    return [item for item in state.get("evidence", []) if item.evidence_id in ids]


def _transport_route(state: ApprovalState) -> tuple[str, list[str]]:
    route = str(_field(state, "route") or "")
    items = _attachment_items(state, "TAXI_INVOICE")
    if not route:
        route = next(
            (match.group() for item in items if (match := _ROUTE_TEXT.search(item.excerpt or ""))),
            "",
        )
    return route, [item.evidence_id for item in items]


def _confirmed_private_commute(state: ApprovalState) -> bool:
    route, _ = _transport_route(state)
    return "住所" in route and "固定办公地点" in route


def required_rule_ids(
    state: ApprovalState, context: ToolExecutionContext
) -> tuple[str, ...]:
    expense_type = state["application"].expense_type
    department_id = state["applicant"].department_id
    department_policy_applies = (
        "POL-DEPT-OPS-MKT-2026-V1" in context.allowed_document_ids
        and department_id in _OPS_MKT_DEPARTMENTS
    )
    if department_policy_applies and expense_type == ExpenseType.TRANSPORT:
        return (
            "RULE-EXPENSE-TYPE-IN-SCOPE",
            "RULE-AMOUNT-DATE-CONSISTENCY",
            "RULE-OPS-MKT-TRANSPORT-LIMIT",
            "RULE-TRANSPORT-RECEIPT",
            "RULE-SUBMISSION-DEADLINE",
        )
    if department_policy_applies and expense_type == ExpenseType.DINING:
        return (
            "RULE-EXPENSE-TYPE-IN-SCOPE",
            "RULE-MEAL-DOCUMENT-CONSISTENCY",
            "RULE-OPS-MKT-MEAL-LIMIT",
        )
    if expense_type != ExpenseType.TRANSPORT:
        required = _REQUIRED_RULE_IDS[expense_type]
        if expense_type == ExpenseType.DINING:
            applicable_versions = {
                rule_id
                for rule_id, document_id in _DINING_VERSION_DOCUMENTS.items()
                if document_id in context.allowed_document_ids
            }
            if applicable_versions:
                return tuple(
                    rule_id
                    for rule_id in required
                    if rule_id not in _DINING_VERSION_DOCUMENTS
                    or rule_id in applicable_versions
                )
        return required
    if (
        department_id == _SALES_DEPARTMENT
        and "POL-DEPT-SALES-TRANSPORT-2026-V1" in context.allowed_document_ids
    ):
        rules = [
            "RULE-EXPENSE-TYPE-IN-SCOPE",
            "RULE-SALES-TICKET-CONSISTENCY",
            "RULE-DUPLICATE-INVOICE",
            "RULE-BUDGET-AVAILABLE",
            "RULE-APPLICANT-PERMISSION",
        ]
        if _confirmed_private_commute(state):
            rules.append("RULE-SALES-PRIVATE-COMMUTE")
        else:
            rules.extend(
                ["RULE-SALES-TAXI-LIMIT", "RULE-CUSTOMER-VISIT-CONFIRMED"]
            )
        return tuple(rules)
    return _REQUIRED_RULE_IDS[expense_type]


def _policy_items(
    state: ApprovalState,
    *,
    document_id: str | None = None,
    keywords: tuple[str, ...] = (),
) -> list[EvidenceItem]:
    return [
        item
        for item in state.get("evidence", [])
        if item.source_type
        in {EvidenceSource.POLICY_DOCUMENT, EvidenceSource.DOCUMENT_CONTEXT}
        and (document_id is None or item.document_id == document_id)
        and (not keywords or any(word in (item.excerpt or "") for word in keywords))
    ]


def _structured_evidence(
    state: ApprovalState,
    store: PostgresStore,
    context: ToolExecutionContext,
    *,
    query_type: str,
    record_key: str,
    use_policy_date: bool = False,
) -> tuple[EvidenceItem | None, list[EvidenceItem]]:
    record_keys = [record_key]
    if query_type == "city_tier" and record_key and not record_key.endswith("市"):
        record_keys.append(f"{record_key}市")
    existing = next(
        (
            item
            for item in state.get("evidence", [])
            if item.source_type == EvidenceSource.STRUCTURED_RECORD
            and item.query_type == query_type
            and item.record_key in record_keys
            and item.structured_data_snapshot_id in context.structured_snapshots
            and (
                query_type != "duplicate_invoice"
                or DEMO_STRUCTURED_SNAPSHOT_ID not in context.structured_snapshots
                or item.fixture_id == f"LIVE-INVOICE-{state['request_id']}"
            )
        ),
        None,
    )
    if existing:
        return existing, []
    found = None
    for key in record_keys:
        found = store.find(
            query_type=query_type,
            record_key=key,
            snapshot_ids=set(context.structured_snapshots),
            as_of=(context.policy_effective_at if use_policy_date else context.structured_as_of),
            request_id=context.request_id,
        )
        if found is not None:
            break
    if found is None:
        return None, []
    item = structured_record_to_evidence(*found)
    return item, [item]


def _catalog(state: ApprovalState, store: PostgresStore) -> dict[str, list[PolicyRuleRecord]]:
    application = state["application"]
    rules = store.policy_rules(
        expense_type=application.expense_type,
        effective_at=application.occurred_on,
        document_ids={
            item.document_id
            for item in state.get("evidence", [])
            if item.document_id
            and item.source_type
            in {EvidenceSource.POLICY_DOCUMENT, EvidenceSource.DOCUMENT_CONTEXT}
        },
    )
    grouped: dict[str, list[PolicyRuleRecord]] = {}
    for rule in rules:
        grouped.setdefault(rule.rule_id, []).append(rule)
    return grouped


def _version(catalog: dict[str, list[PolicyRuleRecord]], rule_id: str) -> str:
    rules = catalog.get(rule_id)
    return rules[0].rule_version if rules else "UNAVAILABLE"


def _scope_rule(catalog: dict[str, list[PolicyRuleRecord]]) -> RuleResult:
    return RuleResult(
        rule_id="RULE-EXPENSE-TYPE-IN-SCOPE",
        rule_version=_version(catalog, "RULE-EXPENSE-TYPE-IN-SCOPE"),
        producer="RULE_VALIDATOR",
        outcome=RuleOutcome.PASS,
        input_refs=["input.application.expense_type"],
    )


def _document_consistency(
    state: ApprovalState,
    catalog: dict[str, list[PolicyRuleRecord]],
    *,
    rule_id: str,
    fields: tuple[str, ...],
    application_values: tuple[object | None, ...],
    document_types: tuple[str, ...],
) -> RuleResult:
    extracted = tuple(_field(state, name) for name in fields)
    missing_document = any(value is None for value in extracted)
    missing_application = any(value is None for value in application_values)

    def matches_field(name: str, actual: object, expected: object) -> bool:
        if name == "amount":
            try:
                return Decimal(str(actual)) == Decimal(str(expected))
            except InvalidOperation:
                return False
        if name == "city":
            return str(actual).strip().removesuffix("市") == str(expected).strip().removesuffix("市")
        return str(actual) == str(expected)

    matches = not (missing_document or missing_application) and all(
        matches_field(name, actual, expected)
        for name, actual, expected in zip(fields, extracted, application_values, strict=True)
    )
    return RuleResult(
        rule_id=rule_id,
        rule_version=_version(catalog, rule_id),
        producer="RULE_VALIDATOR",
        outcome=(
            RuleOutcome.INDETERMINATE
            if missing_document or missing_application
            else RuleOutcome.PASS
            if matches
            else RuleOutcome.FAIL
        ),
        reason_code=(
            "APPLICATION_FIELDS_MISSING"
            if missing_application
            else "DOCUMENT_FIELDS_MISSING"
            if missing_document
            else None
            if matches
            else "DOCUMENT_APPLICATION_MISMATCH"
        ),
        input_refs=[f"extracted_fields.{name}" for name in fields],
        evidence_ids=_attachment_ids(state, *document_types),
    )


def _transport_amount_limit(
    state: ApprovalState,
    catalog: dict[str, list[PolicyRuleRecord]],
    *,
    rule_id: str,
    document_id: str,
) -> RuleResult:
    application = state["application"]
    rule = catalog[rule_id][0]
    limit = Decimal(str(rule.parameters["limit"]))
    policy_ids = [
        item.evidence_id
        for item in _policy_items(
            state,
            document_id=document_id,
            keywords=(str(limit), f"{limit:.2f}"),
        )[:1]
    ]
    return RuleResult(
        rule_id=rule_id,
        rule_version=rule.rule_version,
        producer="RULE_VALIDATOR",
        outcome=(
            RuleOutcome.PASS if application.amount <= limit else RuleOutcome.FAIL
        ),
        reason_code=(
            None if application.amount <= limit else "TRANSPORT_LIMIT_EXCEEDED"
        ),
        input_refs=["input.application.amount"],
        evidence_ids=policy_ids + _attachment_ids(state, "TAXI_INVOICE")[:1],
        computed_limit=limit,
        actual_amount=application.amount,
    )


def _transport_rules(
    state: ApprovalState,
    store: PostgresStore,
    context: ToolExecutionContext,
    catalog: dict[str, list[PolicyRuleRecord]],
) -> tuple[list[RuleResult], list[EvidenceItem], list[str]]:
    application = state["application"]
    department_id = state["applicant"].department_id
    is_sales = department_id == _SALES_DEPARTMENT
    route, route_evidence_ids = _transport_route(state)
    private_commute = _confirmed_private_commute(state)
    consistency_rule_id = (
        "RULE-SALES-TICKET-CONSISTENCY"
        if is_sales
        else "RULE-AMOUNT-DATE-CONSISTENCY"
    )
    consistency = _document_consistency(
        state,
        catalog,
        rule_id=consistency_rule_id,
        fields=("amount", "occurred_on"),
        application_values=(application.amount, application.occurred_on),
        document_types=("TAXI_INVOICE",),
    )
    if not route.strip():
        consistency = consistency.model_copy(
            update={
                "outcome": RuleOutcome.INDETERMINATE,
                "reason_code": "TRANSPORT_ROUTE_MISSING",
            }
        )

    results = [consistency]
    new_evidence: list[EvidenceItem] = []
    flags: list[str] = []
    if "RULE-TRANSPORT-RECEIPT" in catalog:
        receipt_ids = _attachment_ids(state, "TAXI_INVOICE")
        policy = _policy_items(
            state,
            document_id=catalog["RULE-TRANSPORT-RECEIPT"][0].source_document_id,
            keywords=("缺少交通票据",),
        )
        results.append(
            RuleResult(
                rule_id="RULE-TRANSPORT-RECEIPT",
                rule_version=_version(catalog, "RULE-TRANSPORT-RECEIPT"),
                producer="RULE_VALIDATOR",
                outcome=RuleOutcome.PASS if receipt_ids else RuleOutcome.INDETERMINATE,
                reason_code=None if receipt_ids else "MISSING_RECEIPT",
                input_refs=["input.application.expense_type"],
                evidence_ids=receipt_ids[:1] or [item.evidence_id for item in policy[:1]],
            )
        )
        if not receipt_ids:
            flags.append("MISSING_RECEIPT")
    if "RULE-SUBMISSION-DEADLINE" in catalog:
        deadline = catalog["RULE-SUBMISSION-DEADLINE"][0]
        days = (application.submitted_on - application.occurred_on).days
        limit = int(deadline.parameters["days"])
        policy = _policy_items(
            state,
            document_id=deadline.source_document_id,
            keywords=(f"{limit} 个日历日",),
        )
        results.append(
            RuleResult(
                rule_id=deadline.rule_id,
                rule_version=deadline.rule_version,
                producer="RULE_VALIDATOR",
                outcome=RuleOutcome.PASS if days <= limit else RuleOutcome.FAIL,
                reason_code=None if days <= limit else "LATE_REQUIRES_HUMAN_EXCEPTION",
                input_refs=["input.application.occurred_on", "input.application.submitted_on"],
                evidence_ids=[item.evidence_id for item in policy[:1]],
                computed_value=days,
                computed_limit=Decimal(limit),
                unit="days",
            )
        )
        if days > limit:
            flags.append("OVERDUE_EXCEPTION_REQUIRED")
    if is_sales and private_commute:
        policy = [
            item
            for item in _policy_items(
                state,
                document_id="POL-DEPT-SALES-TRANSPORT-2026-V1",
                keywords=("日常私人通勤",),
            )
            if "不予报销" in (item.excerpt or "")
        ]
        if not policy:
            policy = [
                item
                for item in _policy_items(
                    state,
                    document_id="POL-TRANSPORT-2026-V1",
                    keywords=("日常通勤",),
                )
                if "不予报销" in (item.excerpt or "")
            ]
        results.append(
            RuleResult(
                rule_id="RULE-SALES-PRIVATE-COMMUTE",
                rule_version=_version(catalog, "RULE-SALES-PRIVATE-COMMUTE"),
                producer="RULE_VALIDATOR",
                outcome=RuleOutcome.FAIL,
                reason_code="CONFIRMED_PRIVATE_COMMUTE",
                input_refs=["extracted_fields.route"],
                evidence_ids=[item.evidence_id for item in policy[:1]]
                + route_evidence_ids[:1],
            )
        )
        flags.append("CONFIRMED_PRIVATE_COMMUTE")
    elif not is_sales and "RULE-TRANSPORT-PURPOSE-ELIGIBILITY" in catalog:
        source_document_id = catalog["RULE-TRANSPORT-PURPOSE-ELIGIBILITY"][0].source_document_id
        policy = [
            item
            for item in _policy_items(
                state,
                document_id=source_document_id,
                keywords=("日常通勤",),
            )
            if "不予报销" in (item.excerpt or "")
        ]
        results.append(
            RuleResult(
                rule_id="RULE-TRANSPORT-PURPOSE-ELIGIBILITY",
                rule_version=_version(
                    catalog, "RULE-TRANSPORT-PURPOSE-ELIGIBILITY"
                ),
                producer="RULE_VALIDATOR",
                outcome=(
                    RuleOutcome.INDETERMINATE
                    if not route.strip()
                    else RuleOutcome.FAIL
                    if private_commute
                    else RuleOutcome.PASS
                ),
                reason_code=(
                    "TRANSPORT_ROUTE_MISSING"
                    if not route.strip()
                    else "DAILY_COMMUTE_EXPLICITLY_PROHIBITED"
                    if private_commute
                    else None
                ),
                input_refs=["extracted_fields.route"],
                evidence_ids=[item.evidence_id for item in policy[:1]]
                + route_evidence_ids[:1],
            )
        )

    if private_commute and not is_sales:
        return results, new_evidence, flags

    if is_sales and not private_commute:
        results.append(
            _transport_amount_limit(
                state,
                catalog,
                rule_id="RULE-SALES-TAXI-LIMIT",
                document_id="POL-DEPT-SALES-TRANSPORT-2026-V1",
            )
        )
    elif department_id in _OPS_MKT_DEPARTMENTS:
        results.append(
            _transport_amount_limit(
                state,
                catalog,
                rule_id="RULE-OPS-MKT-TRANSPORT-LIMIT",
                document_id="POL-DEPT-OPS-MKT-2026-V1",
            )
        )

    if is_sales:
        financial_results, added = _financial_fact_rules(
            state, store, context, catalog
        )
        results.extend(financial_results)
        new_evidence.extend(added)
        if not private_commute:
            visit, added = _structured_evidence(
                state,
                store,
                context,
                query_type="customer_visit_record",
                record_key=state["request_id"],
                use_policy_date=True,
            )
            new_evidence.extend(added)
            results.append(
                _fact_result(
                    rule_id="RULE-CUSTOMER-VISIT-CONFIRMED",
                    version=_version(catalog, "RULE-CUSTOMER-VISIT-CONFIRMED"),
                    item=visit,
                    passed=(
                        visit is not None
                        and str(visit.value).endswith(":CONFIRMED")
                    ),
                    input_refs=["input.application.request_id"],
                    fail_code="CUSTOMER_VISIT_NOT_CONFIRMED",
                    computed_value=visit.value if visit else None,
                )
            )
    return results, new_evidence, flags


def _fact_result(
    *,
    rule_id: str,
    version: str,
    item: EvidenceItem | None,
    passed: bool,
    input_refs: list[str],
    fail_code: str,
    computed_value: str | int | Decimal | None = None,
) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        rule_version=version,
        producer="RULE_VALIDATOR",
        outcome=(
            RuleOutcome.INDETERMINATE
            if item is None
            else RuleOutcome.PASS
            if passed
            else RuleOutcome.FAIL
        ),
        reason_code="STRUCTURED_FACT_MISSING" if item is None else None if passed else fail_code,
        input_refs=input_refs,
        evidence_ids=[item.evidence_id] if item else [],
        computed_value=computed_value,
    )


def _financial_fact_rules(
    state: ApprovalState,
    store: PostgresStore,
    context: ToolExecutionContext,
    catalog: dict[str, list[PolicyRuleRecord]],
) -> tuple[list[RuleResult], list[EvidenceItem]]:
    application = state["application"]
    new_evidence: list[EvidenceItem] = []
    duplicate, added = _structured_evidence(
        state,
        store,
        context,
        query_type="duplicate_invoice",
        record_key=str(_field(state, "invoice_number") or ""),
    )
    new_evidence.extend(added)
    budget, added = _structured_evidence(
        state,
        store,
        context,
        query_type="budget_status",
        record_key=f"{state['applicant'].department_id}:{application.submitted_on:%Y-%m}",
    )
    new_evidence.extend(added)
    permission, added = _structured_evidence(
        state,
        store,
        context,
        query_type="approval_permission",
        record_key=f"{state['applicant'].employee_id}:{application.expense_type.value}",
    )
    new_evidence.extend(added)
    return (
        [
            _fact_result(
                rule_id="RULE-DUPLICATE-INVOICE",
                version=_version(catalog, "RULE-DUPLICATE-INVOICE"),
                item=duplicate,
                passed=duplicate is not None and duplicate.value is False,
                input_refs=["extracted_fields.invoice_number"],
                fail_code="DUPLICATE_INVOICE_FOUND",
            ),
            _fact_result(
                rule_id="RULE-BUDGET-AVAILABLE",
                version=_version(catalog, "RULE-BUDGET-AVAILABLE"),
                item=budget,
                passed=(
                    budget is not None
                    and budget.value == "AVAILABLE"
                    and budget.available_amount is not None
                    and budget.available_amount >= application.amount
                ),
                input_refs=[
                    "input.applicant.department_id",
                    "input.application.amount",
                ],
                fail_code="BUDGET_NOT_AVAILABLE",
            ),
            _fact_result(
                rule_id="RULE-APPLICANT-PERMISSION",
                version=_version(catalog, "RULE-APPLICANT-PERMISSION"),
                item=permission,
                passed=permission is not None and permission.value == "AUTHORIZED",
                input_refs=[
                    "input.applicant.employee_id",
                    "input.application.expense_type",
                ],
                fail_code="APPLICANT_NOT_AUTHORIZED",
            ),
        ],
        new_evidence,
    )


def _lodging_rules(
    state: ApprovalState,
    store: PostgresStore,
    context: ToolExecutionContext,
    catalog: dict[str, list[PolicyRuleRecord]],
) -> tuple[list[RuleResult], list[EvidenceItem]]:
    application = state["application"]
    nights = (
        (application.check_out - application.check_in).days
        if application.check_in and application.check_out
        else 0
    )
    room_count = application.room_count or 0
    itinerary_ids = _attachment_ids(state, "ITINERARY")
    night_result = RuleResult(
        rule_id="RULE-LODGING-NIGHT-COUNT",
        rule_version=_version(catalog, "RULE-LODGING-NIGHT-COUNT"),
        producer="RULE_VALIDATOR",
        outcome=RuleOutcome.PASS if nights > 0 and room_count > 0 else RuleOutcome.INDETERMINATE,
        reason_code=None if nights > 0 and room_count > 0 else "STAY_DATES_MISSING",
        input_refs=[
            "input.application.check_in",
            "input.application.check_out",
            "input.application.room_count",
        ],
        evidence_ids=itinerary_ids,
        computed_value=nights * room_count if nights and room_count else None,
    )

    new_evidence: list[EvidenceItem] = []
    city_item, added = _structured_evidence(
        state,
        store,
        context,
        query_type="city_tier",
        record_key=application.city or "",
        use_policy_date=True,
    )
    new_evidence.extend(added)
    limit_rule = catalog["RULE-LODGING-AMOUNT-LIMIT"][0]
    per_night = (
        Decimal(str(limit_rule.parameters.get(str(city_item.value)))) if city_item else None
    )
    computed_limit = per_night * nights * room_count if per_night else None
    policy_ids = [
        item.evidence_id
        for item in _policy_items(
            state,
            document_id=limit_rule.source_document_id,
            keywords=(
                str(limit_rule.parameters.get("A", "")),
                str(limit_rule.parameters.get("B", "")),
                "住宿标准",
            ),
        )[:1]
    ]
    amount_result = RuleResult(
        rule_id=limit_rule.rule_id,
        rule_version=limit_rule.rule_version,
        producer="RULE_VALIDATOR",
        outcome=(
            RuleOutcome.INDETERMINATE
            if computed_limit is None
            else RuleOutcome.PASS
            if application.amount <= computed_limit
            else RuleOutcome.FAIL
        ),
        reason_code=(
            "CITY_TIER_OR_LIMIT_MISSING"
            if computed_limit is None
            else None
            if application.amount <= computed_limit
            else "LODGING_LIMIT_EXCEEDED"
        ),
        input_refs=[
            "input.application.amount",
            "input.application.room_count",
            "input.application.check_in",
            "input.application.check_out",
        ],
        evidence_ids=policy_ids
        + ([city_item.evidence_id] if city_item else [])
        + itinerary_ids,
        computed_limit=computed_limit,
        actual_amount=application.amount,
    )
    document_result = _document_consistency(
        state,
        catalog,
        rule_id="RULE-LODGING-DOCUMENT-CONSISTENCY",
        fields=("amount", "city", "check_in", "check_out", "room_count"),
        application_values=(
            application.amount,
            application.city,
            application.check_in,
            application.check_out,
            application.room_count,
        ),
        document_types=("HOTEL_INVOICE", "ITINERARY"),
    )

    submit_rule = catalog["RULE-SUBMISSION-TIMELINESS"][0]
    elapsed = (
        (application.submitted_on - application.check_out).days
        if application.check_out
        else None
    )
    day_limit = int(submit_rule.parameters["days"])
    timeliness = RuleResult(
        rule_id=submit_rule.rule_id,
        rule_version=submit_rule.rule_version,
        producer="RULE_VALIDATOR",
        outcome=(
            RuleOutcome.INDETERMINATE
            if elapsed is None
            else RuleOutcome.PASS
            if elapsed <= day_limit
            else RuleOutcome.FAIL
        ),
        reason_code=(
            "CHECK_OUT_MISSING"
            if elapsed is None
            else None
            if elapsed <= day_limit
            else "SUBMISSION_LATE"
        ),
        input_refs=["input.application.check_out", "input.application.submitted_on"],
        evidence_ids=[
            item.evidence_id
            for item in _policy_items(
                state,
                document_id=submit_rule.source_document_id,
                keywords=(f"{day_limit} 个日历日", "提交"),
            )[:1]
        ],
        computed_value=elapsed,
        computed_limit=Decimal(day_limit),
        unit="days",
    )

    structured_results, added = _financial_fact_rules(state, store, context, catalog)
    new_evidence.extend(added)
    return [night_result, amount_result, document_result, timeliness, *structured_results], new_evidence


def _dining_rules(
    state: ApprovalState, catalog: dict[str, list[PolicyRuleRecord]]
) -> tuple[list[RuleResult], list[str]]:
    application = state["application"]
    document_result = _document_consistency(
        state,
        catalog,
        rule_id="RULE-MEAL-DOCUMENT-CONSISTENCY",
        fields=("amount", "occurred_on"),
        application_values=(application.amount, application.occurred_on),
        document_types=("MEAL_INVOICE",),
    )
    if document_result.outcome == RuleOutcome.PASS:
        invoice_count = _field(state, "attendee_count")
        reason_code = None
        if not isinstance(invoice_count, int) or application.attendee_count is None:
            reason_code = "ATTENDEE_COUNT_MISSING"
        elif invoice_count != application.attendee_count:
            reason_code = "ATTENDEE_COUNT_CONFLICT"
        if reason_code:
            document_result = document_result.model_copy(
                update={
                    "outcome": RuleOutcome.INDETERMINATE,
                    "reason_code": reason_code,
                    "input_refs": [
                        *document_result.input_refs,
                        "extracted_fields.attendee_count",
                        "input.application.attendee_count",
                    ],
                }
            )
    if "RULE-OPS-MKT-MEAL-LIMIT" in catalog:
        rule = catalog["RULE-OPS-MKT-MEAL-LIMIT"][0]
        attendee_count = application.attendee_count or 0
        limit = Decimal(str(rule.parameters["per_person"])) * attendee_count if attendee_count else None
        policy = _policy_items(
            state,
            document_id=rule.source_document_id,
            keywords=(str(rule.parameters["per_person"]),),
        )
        limit_result = RuleResult(
            rule_id=rule.rule_id,
            rule_version=rule.rule_version,
            producer="RULE_VALIDATOR",
            outcome=(
                RuleOutcome.INDETERMINATE if limit is None
                else RuleOutcome.PASS if application.amount <= limit
                else RuleOutcome.FAIL
            ),
            reason_code=(
                "ATTENDEE_COUNT_MISSING" if limit is None
                else None if application.amount <= limit
                else "MEAL_LIMIT_EXCEEDED"
            ),
            input_refs=["input.application.amount", "input.application.attendee_count"],
            evidence_ids=[item.evidence_id for item in policy[:1]],
            computed_limit=limit,
            actual_amount=application.amount,
        )
        return [document_result, limit_result], []
    limit_rules = sorted(
        [rule for rules in catalog.values() for rule in rules if rule.rule_type == "amount_limit"],
        key=lambda item: item.effective_from,
    )
    context_ids = [
        items[0].evidence_id
        for document_id in {rule.source_document_id for rule in limit_rules}
        if document_id
        and (items := _policy_items(state, document_id=document_id, keywords=("施行", "有效期")))
    ]
    attendee_count = application.attendee_count or 0
    limit_results: list[RuleResult] = []
    for rule in limit_rules:
        per_person = Decimal(str(rule.parameters["per_person"]))
        limit = per_person * attendee_count if attendee_count else None
        evidence = _policy_items(
            state,
            document_id=rule.source_document_id,
            keywords=(f"{per_person:.2f}", "每人每日"),
        )
        limit_results.append(
            RuleResult(
                rule_id=rule.rule_id,
                rule_version=rule.rule_version,
                producer="RULE_VALIDATOR",
                outcome=(
                    RuleOutcome.INDETERMINATE
                    if limit is None
                    else RuleOutcome.PASS
                    if application.amount <= limit
                    else RuleOutcome.FAIL
                ),
                reason_code=(
                    "ATTENDEE_COUNT_MISSING"
                    if limit is None
                    else None
                    if application.amount <= limit
                    else "MEAL_LIMIT_EXCEEDED"
                ),
                input_refs=["input.application.amount", "input.application.attendee_count"],
                evidence_ids=[item.evidence_id for item in evidence[:1]],
                computed_limit=limit,
                actual_amount=application.amount,
            )
        )
    conflicting = len({item.outcome for item in limit_results}) > 1
    # Overlapping versions block a recommendation only when they disagree on the result.
    version_result = RuleResult(
        rule_id="RULE-POLICY-VERSION-UNIQUENESS",
        rule_version=_version(catalog, "RULE-POLICY-VERSION-UNIQUENESS"),
        producer="RULE_VALIDATOR",
        outcome=RuleOutcome.FAIL if conflicting else RuleOutcome.PASS,
        reason_code="MULTIPLE_PUBLISHED_VERSIONS_APPLY" if conflicting else None,
        input_refs=["input.application.occurred_on", "input.application.expense_type"],
        evidence_ids=context_ids,
    )
    determination = RuleResult(
        rule_id="RULE-MEAL-LIMIT-DETERMINATION",
        rule_version=_version(catalog, "RULE-MEAL-LIMIT-DETERMINATION"),
        producer="RULE_VALIDATOR",
        outcome=RuleOutcome.INDETERMINATE if conflicting else limit_results[0].outcome,
        reason_code="AUTHORITATIVE_PRECEDENCE_MISSING" if conflicting else None,
        input_refs=[
            "input.application.amount",
            "input.application.occurred_on",
            "input.application.attendee_count",
        ],
        evidence_ids=[
            evidence_id
            for result in limit_results
            for evidence_id in result.evidence_ids
        ]
        + context_ids,
    )
    flags = (
        [
            "RULE_VERSION_CONFLICT",
            "MATERIAL_OUTCOME_DIVERGENCE",
            "MISSING_AUTHORITATIVE_PRECEDENCE",
        ]
        if conflicting
        else []
    )
    return [document_result, version_result, *limit_results, determination], flags


def _metrics(state: ApprovalState, results: list[RuleResult], flags: list[str]) -> EvidenceMetrics:
    coverage = state.get("evidence_review").coverage if state.get("evidence_review") else []
    cited = [item for item in results if item.rule_id != "RULE-EXPENSE-TYPE-IN-SCOPE"]
    consistency = [item for item in results if "CONSISTENCY" in item.rule_id]
    extracted = state.get("extracted_fields", [])
    return EvidenceMetrics(
        evidence_coverage=(
            sum(item.status.value == "SUPPORTED" for item in coverage) / len(coverage)
            if coverage
            else None
        ),
        citation_completeness=(
            sum(bool(item.evidence_ids) for item in cited) / len(cited) if cited else None
        ),
        field_consistency=(
            sum(item.outcome == RuleOutcome.PASS for item in consistency) / len(consistency)
            if consistency
            else None
        ),
        retrieval_margin=None,
        rule_conflict_count=1 if "RULE_VERSION_CONFLICT" in flags else 0,
        extraction_quality=(
            sum(item.status == ExtractionStatus.PRESENT for item in extracted) / len(extracted)
            if extracted
            else None
        ),
    )


def evaluate_rules(
    state: ApprovalState,
    store: PostgresStore,
    context: ToolExecutionContext,
) -> RuleEvaluation:
    catalog = _catalog(state, store)
    expense_type = state["application"].expense_type
    required_ids = required_rule_ids(state, context)
    missing_rule_ids = [rule_id for rule_id in required_ids if not catalog.get(rule_id)]
    if missing_rule_ids:
        results = (
            [_scope_rule(catalog)]
            if catalog.get("RULE-EXPENSE-TYPE-IN-SCOPE")
            else []
        ) + [
            RuleResult(
                rule_id=rule_id,
                rule_version="UNAVAILABLE",
                producer="RULE_VALIDATOR",
                outcome=RuleOutcome.INDETERMINATE,
                reason_code="RULE_CATALOG_ENTRY_MISSING",
            )
            for rule_id in missing_rule_ids
        ]
        flags = ["RULE_CATALOG_INCOMPLETE"]
        return RuleEvaluation(
            results=results,
            new_evidence=[],
            risk_flags=flags,
            metrics=_metrics(state, results, flags),
        )

    results = [_scope_rule(catalog)]
    new_evidence: list[EvidenceItem] = []
    flags: list[str] = []
    if expense_type == ExpenseType.TRANSPORT:
        transport, added, flags = _transport_rules(state, store, context, catalog)
        results.extend(transport)
        new_evidence.extend(added)
    elif expense_type == ExpenseType.LODGING:
        lodging, added = _lodging_rules(state, store, context, catalog)
        results.extend(lodging)
        new_evidence.extend(added)
        if any(item.reason_code == "SUBMISSION_LATE" for item in lodging):
            flags.append("OVERDUE_EXCEPTION_REQUIRED")
        if (
            any(item.reason_code == "LODGING_LIMIT_EXCEEDED" for item in lodging)
            and "会议" in state["application"].description
            and any("会议" in (item.excerpt or "") for item in _attachment_items(state, "HOTEL_INVOICE"))
            and _policy_items(state, keywords=("会议主办方指定酒店", "批准例外"))
        ):
            flags.append("LODGING_EXCEPTION_REQUIRES_REVIEW")
    elif expense_type == ExpenseType.DINING:
        dining, flags = _dining_rules(state, catalog)
        results.extend(dining)
    return RuleEvaluation(
        results=results,
        new_evidence=new_evidence,
        risk_flags=flags,
        metrics=_metrics(state, results, flags),
    )
