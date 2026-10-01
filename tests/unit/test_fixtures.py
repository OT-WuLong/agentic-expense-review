import hashlib
import json
import re
from datetime import date
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
GOLD = json.loads((ROOT / "data/fixtures/golden_cases.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((ROOT / "data/fixtures/source_manifest.json").read_text(encoding="utf-8"))
STRUCTURED = json.loads((ROOT / "data/fixtures/structured_records.json").read_text(encoding="utf-8"))
REFERENCE_STRUCTURED = json.loads(
    (ROOT / "data/fixtures/structured_records_reference.json").read_text(encoding="utf-8")
)
DOCUMENTS = {
    item["document_id"]: item for item in MANIFEST["policies"] + MANIFEST["attachments"]
}
RECORDS = {item["fixture_id"]: item for item in STRUCTURED["records"]}


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def page_text(document: dict, page: int) -> str:
    reader = PdfReader(ROOT / document["pdf_path"])
    assert not reader.is_encrypted
    assert len(reader.pages) == document["page_count"]
    return compact(reader.pages[page - 1].extract_text() or "")


def test_all_authored_documents_exist_with_real_pdf_pages() -> None:
    assert len(DOCUMENTS) == len(MANIFEST["policies"] + MANIFEST["attachments"])
    assert len(RECORDS) == len(STRUCTURED["records"])
    assert len(MANIFEST["policies"]) == 6
    assert len(MANIFEST["attachments"]) == 12
    assert MANIFEST["company"] == {
        "company_id": "COMPANY-DEMO-001",
        "display_name": "合成企业甲",
        "synthetic": True,
        "deployment_scope": "SINGLE_COMPANY_MVP",
    }
    assert "tenant_id" not in json.dumps(MANIFEST, ensure_ascii=False)
    for document in DOCUMENTS.values():
        source = ROOT / document["authoring_path"]
        pdf = ROOT / document["pdf_path"]
        assert source.is_file() and pdf.is_file()
        assert len(re.findall(r"^<!-- PAGE \d+ -->$", source.read_text(encoding="utf-8"), re.MULTILINE)) == document[
            "page_count"
        ]
        assert len(PdfReader(pdf).pages) == document["page_count"]
        assert hashlib.sha256(pdf.read_bytes()).hexdigest() == document["pdf_sha256"]
        assert document["license_id"] and document["synthetic"] is True
    for policy in MANIFEST["policies"]:
        assert policy["department_ids"] and policy["expense_types"]
        assert set(policy["expense_types"]) <= {"交通", "住宿", "餐饮"}
        assert date.fromisoformat(policy["effective_from"])
        if policy["effective_to"] is not None:
            assert date.fromisoformat(policy["effective_to"]) >= date.fromisoformat(
                policy["effective_from"]
            )
        assert policy["published_status"] == "PUBLISHED"


def test_each_golden_reference_reaches_its_actual_source_page_or_record() -> None:
    assert GOLD["status"] == "FROZEN"
    assert GOLD["data_classification"] == "SYNTHETIC"
    assert MANIFEST["golden_policy_catalog_snapshot_id"] == "POLICY-CATALOG-SYN-1.1.0"
    assert STRUCTURED["snapshot_id"] == MANIFEST["golden_structured_data_snapshot_id"]
    for case in GOLD["cases"]:
        assert case["fixture_context"]["policy_catalog_snapshot_id"] == MANIFEST[
            "golden_policy_catalog_snapshot_id"
        ]
        assert case["fixture_context"]["structured_data_snapshot_id"] == STRUCTURED["snapshot_id"]
        for attachment in case["input"]["documents"]:
            fixture = DOCUMENTS[attachment["document_id"]]
            assert fixture["fixture_key"] == attachment["fixture_key"]
            assert fixture["document_type"] == attachment["document_type"]
            assert fixture["media_type"] == attachment["media_type"]
        for evidence in case["critical_evidence"]:
            if evidence["source_type"] == "STRUCTURED_RECORD":
                fixture = RECORDS[evidence["fixture_id"]]
                for key in ("query_type", "record_key", "snapshot_version", "effective_at", "value"):
                    assert fixture[key] == evidence[key]
                if "available_amount" in evidence:
                    assert fixture["available_amount"] == evidence["available_amount"]
                if "consumer" in evidence:
                    assert fixture["consumer"] == evidence["consumer"]
                assert "page" not in fixture and "document_id" not in fixture
                assert evidence["structured_data_snapshot_id"] == STRUCTURED["snapshot_id"]
                continue

            document = DOCUMENTS[evidence["document_id"]]
            assert document["version"] == evidence["version"]
            for key in ("effective_from", "effective_to", "published_status", "authority_level", "priority", "supersedes_document_id"):
                if key in evidence:
                    assert document[key] == evidence[key]
            assert compact(evidence["excerpt"]) in page_text(document, evidence["page"])
            if evidence.get("section"):
                assert compact(evidence["section"]) in page_text(document, evidence["page"])
        for field in case["expected_extracted_fields"]:
            document = DOCUMENTS[field["document_id"]]
            assert compact(str(field["value"])) in page_text(document, field["page"])


def test_dining_version_conflict_is_not_silently_resolved() -> None:
    old = DOCUMENTS["POL-DINING-2025-V1"]
    new = DOCUMENTS["POL-DINING-2026-V2"]
    occurred_on = date(2026, 6, 15)
    assert date.fromisoformat(old["effective_from"]) <= occurred_on <= date.fromisoformat(
        old["effective_to"]
    )
    assert date.fromisoformat(new["effective_from"]) <= occurred_on
    assert old["authority_level"] == new["authority_level"] == "FORMAL_POLICY"
    assert old["priority"] == new["priority"] == 100
    assert old["supersedes_document_id"] is new["supersedes_document_id"] is None
    assert "120.00" in page_text(old, 4) and "150.00" in page_text(new, 4)
    assert "例外" in page_text(DOCUMENTS["POL-LODGING-2026-V3"], 5)


def test_policies_have_enterprise_structure_without_runtime_instructions() -> None:
    forbidden = ("自动预审", "模型", "RAG", "快照版本", "技术错误", "合成制度：")
    for policy in MANIFEST["policies"]:
        source = (ROOT / policy["authoring_path"]).read_text(encoding="utf-8")
        section_count = len(re.findall(r"^## ", source, re.MULTILINE))
        assert 4 <= policy["page_count"] <= 6
        assert section_count >= policy["page_count"] + 2
        assert "归口部门" in source and "批准主体" in source
        assert ("凭证" in source or "票据" in source) and ("归档" in source or "档案" in source)
        assert not any(term in source for term in forbidden)
    lodging = (ROOT / DOCUMENTS["POL-LODGING-2026-V3"]["authoring_path"]).read_text(
        encoding="utf-8"
    )
    supplement = (
        ROOT / DOCUMENTS["POL-DEPT-OPS-MKT-2026-V1"]["authoring_path"]
    ).read_text(encoding="utf-8")
    assert "| 城市等级 | 每间夜上限 | 核算单位 |" in lodging
    assert "| 例外情形 | 必要材料 | 批准角色 |" in supplement
    assert "城市等级每间夜上限核算单位" in page_text(DOCUMENTS["POL-LODGING-2026-V3"], 5)
    assert "例外情形必要材料批准角色" in page_text(
        DOCUMENTS["POL-DEPT-OPS-MKT-2026-V1"], 4
    )


def test_reference_informed_policy_remains_fictional_and_separate() -> None:
    assert REFERENCE_STRUCTURED["snapshot_id"] == MANIFEST[
        "reference_structured_data_snapshot_id"
    ]
    assert len(REFERENCE_STRUCTURED["records"]) == 17
    assert "tenant_id" not in json.dumps(REFERENCE_STRUCTURED, ensure_ascii=False)
    assert all(
        "page" not in record and "document_id" not in record
        for record in REFERENCE_STRUCTURED["records"]
    )
    source_ids = {item["source_id"] for item in MANIFEST["reference_sources"]}
    assert all(item["original_text_imported"] is False for item in MANIFEST["reference_sources"])
    assert {item["document_id"] for item in MANIFEST["declared_external_dependencies"]} == {
        "FIN-GEN-2026",
        "FIN-TRAVEL-2026",
        "FIN-FX-2026",
        "FIN-ARCH-2026",
    }
    expected_departments = {
        "POL-DEPT-OPS-MKT-2026-V1": {"DEPT-MARKETING", "DEPT-OPERATIONS"},
        "POL-DEPT-SALES-TRANSPORT-2026-V1": {"DEPT-SALES"},
    }
    for document_id, departments in expected_departments.items():
        policy = DOCUMENTS[document_id]
        assert set(policy["department_ids"]) == departments
        assert policy["priority"] == 110
        assert policy["catalog_snapshot_id"] == MANIFEST["reference_policy_catalog_snapshot_id"]
        assert policy["source_type"] == "REFERENCE_INFORMED_SYNTHETIC"
        assert policy["authority_level"] == "FORMAL_POLICY"
        assert set(policy["reference_source_ids"]) <= source_ids
        text = "".join(page_text(policy, page) for page in range(1, policy["page_count"] + 1))
        assert "GitLab" not in text and "福瑞" not in text
