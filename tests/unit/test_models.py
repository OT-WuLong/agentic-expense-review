import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models import (
    Application,
    ApprovalInput,
    ApprovalResponse,
    ApprovalStatus,
    DecisionReason,
    EvidenceItem,
    ExtractedField,
    Recommendation,
    RuleResult,
)

GOLDEN_CASES = json.loads(
    (Path(__file__).parents[2] / "data" / "fixtures" / "golden_cases.json").read_text(
        encoding="utf-8"
    )
)["cases"]


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda case: case["case_id"])
def test_golden_cases_parse_as_business_contracts(case: dict) -> None:
    request = ApprovalInput.model_validate(case["input"])
    fields = [ExtractedField.model_validate(item) for item in case["expected_extracted_fields"]]
    # expected_fact is an evaluator-only label, never part of runtime evidence.
    evidence = [
        EvidenceItem.model_validate({key: value for key, value in item.items() if key != "expected_fact"})
        for item in case["critical_evidence"]
    ]
    rules = [RuleResult.model_validate(item) for item in case["expected_rule_results"]]
    recommendation = Recommendation(case["expected_recommendation"])

    assert request.application.amount == Decimal(case["input"]["application"]["amount"])
    assert request.application.occurred_on == date.fromisoformat(
        case["input"]["application"]["occurred_on"]
    )
    assert request.application.currency == "CNY"
    assert all(field.document_id for field in fields)
    assert len(evidence) == len(case["critical_evidence"])
    assert len(rules) == len(case["expected_rule_results"])
    assert ApprovalInput.model_validate_json(request.model_dump_json()) == request

    response = ApprovalResponse(
        request_id=request.request_id,
        trace_id=f"TRACE-{case['case_id']}",
        status=(
            ApprovalStatus.HUMAN_PENDING
            if recommendation == Recommendation.HUMAN_REVIEW
            else ApprovalStatus.COMPLETED
        ),
        recommendation=recommendation,
        reasons=[
            DecisionReason(
                code=case["scenario"],
                message=case["title"],
                evidence_ids=[evidence[0].evidence_id],
                rule_ids=[
                    next(rule.rule_id for rule in rules if rule.outcome == "FAIL")
                    if recommendation == Recommendation.REJECT_RECOMMENDED
                    else rules[0].rule_id
                ],
            )
        ],
        rule_results=rules,
        evidence=evidence,
    )
    assert ApprovalResponse.model_validate_json(response.model_dump_json()) == response


def test_approval_input_rejects_legacy_tenant_scope() -> None:
    with pytest.raises(ValidationError):
        ApprovalInput.model_validate(
            {**GOLDEN_CASES[0]["input"], "tenant_id": "tenant-not-supported"}
        )


@pytest.mark.parametrize("invalid", ["0", "-1", "NaN", "Infinity", "oops", 1.25, True])
def test_application_rejects_invalid_money(invalid: object) -> None:
    application = {**GOLDEN_CASES[0]["input"]["application"], "amount": invalid}
    with pytest.raises(ValidationError):
        Application.model_validate(application)


@pytest.mark.parametrize("invalid", ["2026-02-30", "2026-2-3", "2026-03-12T09:00:00"])
def test_application_rejects_invalid_dates(invalid: str) -> None:
    application = {**GOLDEN_CASES[0]["input"]["application"], "occurred_on": invalid}
    with pytest.raises(ValidationError):
        Application.model_validate(application)


def test_currency_is_normalized_without_claiming_an_iso_catalog() -> None:
    application = {**GOLDEN_CASES[0]["input"]["application"], "currency": " cny "}
    assert Application.model_validate(application).currency == "CNY"


def test_document_evidence_cannot_lose_its_source() -> None:
    source = GOLDEN_CASES[0]["critical_evidence"][0]
    evidence = {key: value for key, value in source.items() if key not in {"expected_fact", "page"}}
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(evidence)


def test_structured_evidence_requires_a_snapshot_not_a_fake_page() -> None:
    source = next(
        item for item in GOLDEN_CASES[1]["critical_evidence"] if item["source_type"] == "STRUCTURED_RECORD"
    )
    evidence = {key: value for key, value in source.items() if key != "expected_fact"}
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate({**evidence, "structured_data_snapshot_id": None})
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate({**evidence, "page": 1})


def test_system_error_is_not_a_business_recommendation() -> None:
    with pytest.raises(ValidationError):
        ApprovalResponse(
            request_id="REQ-1",
            trace_id="TRACE-1",
            status=ApprovalStatus.SYSTEM_ERROR,
            recommendation=Recommendation.REJECT_RECOMMENDED,
        )


def test_positive_response_needs_cited_rules_evidence_and_reason() -> None:
    base = {
        "request_id": "REQ-1",
        "trace_id": "TRACE-1",
        "status": "COMPLETED",
        "recommendation": "PASS_RECOMMENDED",
    }
    with pytest.raises(ValidationError):
        ApprovalResponse.model_validate(base)
    with pytest.raises(ValidationError):
        ApprovalResponse.model_validate(
            {
                **base,
                "reasons": [{"code": "NO_SOURCE", "message": "无证据", "evidence_ids": ["FORGED"]}],
            }
        )


def test_status_and_recommendation_must_form_a_legal_pair() -> None:
    with pytest.raises(ValidationError):
        ApprovalResponse(
            request_id="REQ-1",
            trace_id="TRACE-1",
            status=ApprovalStatus.BUSINESS_REJECTED,
            recommendation=Recommendation.PASS_RECOMMENDED,
        )
    with pytest.raises(ValidationError):
        ApprovalResponse(
            request_id="REQ-1",
            trace_id="TRACE-1",
            status=ApprovalStatus.COMPLETED,
        )


def test_human_review_can_explain_missing_material_without_fabricated_evidence() -> None:
    response = ApprovalResponse(
        request_id="REQ-NEEDS-DOCUMENT",
        trace_id="TRACE-NEEDS-DOCUMENT",
        status=ApprovalStatus.HUMAN_PENDING,
        recommendation=Recommendation.HUMAN_REVIEW,
        reasons=[DecisionReason(code="DOCUMENT_MISSING", message="缺少票据，等待人工复核")],
    )
    assert response.evidence == []
