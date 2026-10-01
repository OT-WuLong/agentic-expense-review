"""A valid fixture and small corruptions that must never enter an evaluation run."""

import json
from copy import deepcopy

import pytest

from scripts.validate_datasets import (
    ROOT,
    freeze_metadata,
    read_bundle,
    validate_bundle,
    validate_freeze,
)


@pytest.fixture(scope="module")
def bundle():
    datasets, manifest, structured, ref_structured, catalog, errors = read_bundle(ROOT)
    assert not errors
    return datasets, manifest, structured, ref_structured, catalog


def check_corruption(bundle, mutate, expected: str) -> None:
    datasets, manifest, structured, ref_structured, catalog = deepcopy(bundle)
    mutate(datasets, manifest, structured, ref_structured, catalog)
    errors = validate_bundle(ROOT, datasets, manifest, structured, ref_structured, catalog)
    assert any(expected in error for error in errors), errors


def check_golden_corruption(bundle, mutate, expected: str) -> None:
    datasets, manifest, structured, ref_structured, catalog = deepcopy(bundle)
    golden = json.loads((ROOT / "data/fixtures/golden_cases.json").read_text(encoding="utf-8"))
    mutate(golden)
    errors = validate_bundle(
        ROOT, datasets, manifest, structured, ref_structured, catalog, golden
    )
    assert any(expected in error for error in errors), errors


def referenced_structured(datasets, catalog, query_type: str) -> dict:
    used_ids = {
        evidence_id
        for approval in datasets["approvals"]
        for evidence_id in approval["expected_evidence_ids"]
    }
    return next(
        item
        for item in catalog["items"]
        if item.get("query_type") == query_type and item["evidence_id"] in used_ids
    )


def test_current_evaluation_data_is_valid(bundle) -> None:
    datasets, manifest, structured, ref_structured, catalog = bundle
    assert not validate_bundle(ROOT, datasets, manifest, structured, ref_structured, catalog)
    freeze = freeze_metadata(ROOT, datasets, manifest, structured, ref_structured, catalog)
    assert freeze["dataset_version"] == datasets["approvals"][0]["dataset_version"]
    assert freeze["datasets"]["approvals"]["test_count"] >= 3


def test_duplicate_sample_id_is_rejected(bundle) -> None:
    def mutate(datasets, *_):
        datasets["retrieval"][1]["sample_id"] = datasets["retrieval"][0]["sample_id"]

    check_corruption(bundle, mutate, "duplicate sample_id")


def test_missing_evidence_and_bad_source_page_are_rejected(bundle) -> None:
    def mutate(datasets, _manifest, _structured, _ref_structured, catalog):
        datasets["approvals"][0]["expected_evidence_ids"] = ["EVID-NOT-REAL"]
        catalog["items"][0]["page"] = 99

    datasets, manifest, structured, ref_structured, catalog = deepcopy(bundle)
    mutate(datasets, manifest, structured, ref_structured, catalog)
    errors = validate_bundle(ROOT, datasets, manifest, structured, ref_structured, catalog)
    assert any("unknown evidence ID" in error for error in errors)
    assert any("invalid source page" in error for error in errors)


def test_policy_evidence_cannot_masquerade_as_attachment(bundle) -> None:
    def mutate(_datasets, _manifest, _structured, _ref_structured, catalog):
        item = next(x for x in catalog["items"] if x["source_type"] == "POLICY_DOCUMENT")
        item["source_type"] = "ATTACHMENT"
        item.pop("effective_from")  # This would bypass policy-date checks without the manifest guard.
        item.pop("catalog_snapshot_id")

    check_corruption(bundle, mutate, "source type does not match manifest document kind")


def test_policy_evidence_section_must_exist_on_declared_page(bundle) -> None:
    def mutate(_datasets, _manifest, _structured, _ref_structured, catalog):
        item = next(x for x in catalog["items"] if x["source_type"] == "POLICY_DOCUMENT")
        item["section"] = "不存在的章节"

    check_corruption(bundle, mutate, "section absent from actual PDF page")


def test_manifest_cannot_claim_fictional_policy_is_real_enterprise_policy(bundle) -> None:
    def mutate(_datasets, manifest, *_):
        manifest["policies"][0]["source_type"] = "REAL_ENTERPRISE_POLICY"

    check_corruption(bundle, mutate, "illegal manifest source_type")


def test_structured_evidence_cannot_fake_pdf_page_or_record_value(bundle) -> None:
    def mutate(_datasets, _manifest, _structured, _ref_structured, catalog):
        item = next(x for x in catalog["items"] if x["source_type"] == "STRUCTURED_RECORD")
        item["page"] = 1
        item["value"] = "FORGED"

    datasets, manifest, structured, ref_structured, catalog = deepcopy(bundle)
    mutate(datasets, manifest, structured, ref_structured, catalog)
    errors = validate_bundle(ROOT, datasets, manifest, structured, ref_structured, catalog)
    assert any("invalid evidence source" in error for error in errors)
    assert any("structured value mismatch" in error for error in errors)


@pytest.mark.parametrize(
    "query_type",
    ["budget_status", "city_tier"],
)
def test_future_dated_structured_evidence_is_rejected(bundle, query_type) -> None:
    def mutate(datasets, _manifest, _structured, ref_structured, catalog):
        if query_type == "city_tier":
            item = next(x for x in catalog["items"] if x.get("query_type") == query_type)
            meal = next(x for x in datasets["approvals"] if x["sample_id"] == "APP-HO-MEAL-PASS")
            meal["expected_evidence_ids"].append(item["evidence_id"])
        else:
            item = referenced_structured(datasets, catalog, query_type)
        record = next(x for x in ref_structured["records"] if x["fixture_id"] == item["fixture_id"])
        item["effective_at"] = record["effective_at"] = "2099-01-01"

    check_corruption(bundle, mutate, "future-dated structured evidence ID")


def test_present_extracted_value_must_appear_on_actual_pdf_page(bundle) -> None:
    def mutate(datasets, *_):
        row = next(x for x in datasets["approvals"] if x["expected_extracted_fields"])
        next(x for x in row["expected_extracted_fields"] if x["field"] == "amount")["value"] = "999.00"

    check_corruption(bundle, mutate, "extracted field value absent from actual PDF page")


def test_approval_cannot_erase_all_evidence_and_rules(bundle) -> None:
    def mutate(datasets, *_):
        row = datasets["approvals"][0]
        row["expected_evidence_ids"] = []
        row["expected_rule_results"] = []

    datasets, manifest, structured, ref_structured, catalog = deepcopy(bundle)
    mutate(datasets, manifest, structured, ref_structured, catalog)
    errors = validate_bundle(ROOT, datasets, manifest, structured, ref_structured, catalog)
    assert any("empty approval evidence gold" in error for error in errors)
    assert any("empty approval rule gold" in error for error in errors)


def test_each_rule_evidence_must_appear_in_approval_gold(bundle) -> None:
    def mutate(datasets, *_):
        row = next(x for x in datasets["approvals"] if x["expected_rule_results"])
        evidence_id = next(
            evidence_id
            for rule in row["expected_rule_results"]
            for evidence_id in rule["evidence_ids"]
        )
        row["expected_evidence_ids"].remove(evidence_id)

    check_corruption(bundle, mutate, "omitted from approval gold")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("amount", "NaN", "invalid amount/date"),
        ("occurred_on", "2026-02-30", "invalid amount/date"),
        ("expected_recommendation", "AUTO_APPROVED", "illegal approval recommendation"),
    ],
)
def test_illegal_amount_date_or_label_is_rejected(bundle, field, value, message) -> None:
    def mutate(datasets, *_):
        row = datasets["approvals"][0]
        if field == "expected_recommendation":
            row[field] = value
        else:
            row["input"]["application"][field] = value

    check_corruption(bundle, mutate, message)


def test_cross_split_clause_family_is_rejected(bundle) -> None:
    def mutate(datasets, *_):
        datasets["retrieval"][2]["clause_family_id"] = datasets["retrieval"][0][
            "clause_family_id"
        ]

    check_corruption(bundle, mutate, "cross-split clause_family_id")


def test_policy_manifest_requires_a_department_scope(bundle) -> None:
    def mutate(_datasets, manifest, *_):
        manifest["policies"][0]["department_ids"] = []

    check_corruption(bundle, mutate, "missing policy scope")


def test_retrieval_requires_a_department_filter(bundle) -> None:
    def mutate(datasets, *_):
        datasets["retrieval"][0]["filters"].pop("department_id")

    check_corruption(bundle, mutate, "missing retrieval department_id")


def test_retrieval_requires_an_expense_type_filter(bundle) -> None:
    def mutate(datasets, *_):
        datasets["retrieval"][0]["filters"].pop("expense_type")

    check_corruption(bundle, mutate, "missing retrieval expense_type")


def test_policy_evidence_must_apply_to_request_department(bundle) -> None:
    def mutate(datasets, manifest, _structured, _ref_structured, catalog):
        row = next(item for item in datasets["retrieval"] if item["relevant_evidence_groups"])
        evidence_id = row["relevant_evidence_groups"][0]["any_of"][0]
        evidence = next(item for item in catalog["items"] if item["evidence_id"] == evidence_id)
        policy = next(
            item for item in manifest["policies"] if item["document_id"] == evidence["document_id"]
        )
        policy["department_ids"] = ["DEPT-NOT-AUTHORIZED"]

    check_corruption(bundle, mutate, "policy evidence outside department scope")


def test_policy_evidence_must_apply_to_request_expense_type(bundle) -> None:
    def mutate(datasets, manifest, _structured, _ref_structured, catalog):
        row = next(item for item in datasets["retrieval"] if item["relevant_evidence_groups"])
        evidence_id = row["relevant_evidence_groups"][0]["any_of"][0]
        evidence = next(item for item in catalog["items"] if item["evidence_id"] == evidence_id)
        policy = next(
            item for item in manifest["policies"] if item["document_id"] == evidence["document_id"]
        )
        policy["expense_types"] = ["不适用费用类型"]

    check_corruption(bundle, mutate, "policy evidence outside expense scope")


def test_golden_document_evidence_must_match_declared_pdf_page(bundle) -> None:
    def mutate(golden):
        evidence = next(
            item
            for case in golden["cases"]
            for item in case["critical_evidence"]
            if item["source_type"] != "STRUCTURED_RECORD"
        )
        evidence["section"] = "不存在的章节"
        evidence["excerpt"] = "不存在的逐字摘录"

    check_golden_corruption(bundle, mutate, "golden section absent from actual PDF page")
    check_golden_corruption(bundle, mutate, "golden excerpt absent from actual PDF page")


def test_golden_structured_evidence_must_match_frozen_record(bundle) -> None:
    def mutate(golden):
        evidence = next(
            item
            for case in golden["cases"]
            for item in case["critical_evidence"]
            if item["source_type"] == "STRUCTURED_RECORD"
        )
        evidence["value"] = "FORGED"

    check_golden_corruption(bundle, mutate, "golden structured value mismatch")


def test_golden_rejects_old_snapshot_id_anywhere(bundle) -> None:
    def mutate(golden):
        golden["cases"][0]["nested_regression_probe"] = {
            "snapshot": "POLICY-CATALOG-SYN-0.0.0"
        }

    check_golden_corruption(bundle, mutate, "old policy snapshot ID")


@pytest.mark.parametrize(
    ("query_type", "record_key", "message"),
    [
        ("budget_status", "DEPT-NOT-AUTHORIZED:2099-01", "structured budget outside department scope"),
        ("approval_permission", "EMP-NOT-AUTHORIZED:交通", "structured permission outside applicant scope"),
        ("duplicate_invoice", "SYN-NOT-THE-INVOICE", "structured duplicate check outside invoice scope"),
        ("customer_visit_record", "REQ-NOT-THE-REQUEST", "structured customer visit outside request scope"),
    ],
)
def test_structured_evidence_must_match_approval_scope(bundle, query_type, record_key, message) -> None:
    def mutate(datasets, _manifest, _structured, ref_structured, catalog):
        evidence = referenced_structured(datasets, catalog, query_type)
        record = next(
            item for item in ref_structured["records"] if item["fixture_id"] == evidence["fixture_id"]
        )
        evidence["record_key"] = record["record_key"] = record_key

    check_corruption(bundle, mutate, message)


def test_no_answer_with_relevant_group_is_rejected(bundle) -> None:
    def mutate(datasets, *_):
        next(row for row in datasets["retrieval"] if row["no_answer"])["no_answer"] = False

    check_corruption(bundle, mutate, "no_answer/groups inconsistency")


def test_security_pair_requires_attack_and_benign(bundle) -> None:
    def mutate(datasets, *_):
        benign = next(row for row in datasets["security"] if row["variant"] == "BENIGN")
        datasets["security"] = [
            row for row in datasets["security"] if row["sample_id"] != benign["sample_id"]
        ]

    check_corruption(bundle, mutate, "requires exactly ATTACK + BENIGN")


def test_security_request_department_must_be_allowed(bundle) -> None:
    def mutate(datasets, *_):
        context = datasets["security"][0]["permission_context"]
        context["allowed_department_ids"] = ["DEPT-NOT-AUTHORIZED"]

    check_corruption(bundle, mutate, "permission department mismatch")


def test_authorization_attack_must_target_an_unauthorized_department(bundle) -> None:
    def mutate(datasets, *_):
        pair = [item for item in datasets["security"] if item["pair_id"] == "SEC-PAIR-HO-SQL"]
        for row in pair:
            row["attack_family_id"] = "AUTHORIZATION_SCOPE_OVERRIDE"
            row["payload"]["filters"] = {
                "department_id": row["permission_context"]["allowed_department_ids"][0]
            }

    check_corruption(bundle, mutate, "authorization attack lacks unauthorized department")


def test_frozen_hash_change_is_rejected(bundle, tmp_path) -> None:
    datasets, manifest, structured, ref_structured, catalog = bundle
    freeze = freeze_metadata(ROOT, datasets, manifest, structured, ref_structured, catalog)
    altered = deepcopy(freeze)
    altered["datasets"]["retrieval"]["test_canonical_sha256"] = "0" * 64
    path = tmp_path / "evals" / "datasets"
    path.mkdir(parents=True)
    (path / "test.freeze.json").write_text(json.dumps(altered), encoding="utf-8")
    assert any("frozen datasets" in error for error in validate_freeze(tmp_path, freeze))


def test_default_requires_a_frozen_test_snapshot(bundle, tmp_path) -> None:
    datasets, manifest, structured, ref_structured, catalog = bundle
    candidate = freeze_metadata(ROOT, datasets, manifest, structured, ref_structured, catalog)
    assert any("missing frozen test snapshot" in error for error in validate_freeze(tmp_path, candidate))
