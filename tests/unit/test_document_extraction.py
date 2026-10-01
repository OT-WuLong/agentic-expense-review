import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_p04_report_separates_clean_and_low_quality_inputs() -> None:
    report = json.loads(
        (ROOT / "evals/reports/p04_extraction.json").read_text(encoding="utf-8")
    )
    summary = report["summary"]
    assert report["status"] == "SMOKE_ONLY"
    assert summary["golden_pdf_evidence_found"] == summary["golden_pdf_evidence_total"] == 11
    assert summary["field_exact_match_count"] == 19
    assert summary["field_expected_count"] == 20
    assert summary["low_text_quality_document_count"] == 1
    assert summary["incomplete_field_document_count"] == 1

    documents = {item["document_id"]: item for item in report["documents"]}
    golden_documents = [
        item
        for document_id, item in documents.items()
        if document_id.startswith("DOC-GC-")
    ]
    assert sum(item["expected_field_matches"] for item in golden_documents) == 14
    assert sum(item["expected_field_count"] for item in golden_documents) == 14
    assert documents["P04-CLEAR-IMAGE-TAXI-001"]["quality_status"] == "PASS"
    assert documents["P04-LOW-QUALITY-SCAN-TAXI-001"]["quality_status"] == "LOW_TEXT_QUALITY"
    assert documents["P04-LOW-QUALITY-SCAN-TAXI-001"]["model_version"] == "vlm"

    low_quality_invoice = next(
        item
        for item in report["fields"]
        if item["document_id"] == "P04-LOW-QUALITY-SCAN-TAXI-001"
        and item["field"] == "invoice_number"
    )
    assert low_quality_invoice["status"] == "UNREADABLE"
    assert low_quality_invoice["value"] is None
    assert low_quality_invoice["page"] == 1 and low_quality_invoice["bbox"]
    assert "CROSS_FIELD_DATE_MISMATCH" in low_quality_invoice["quality_flags"]
    invoice_metrics = report["field_metrics"]["invoice_number"]
    assert invoice_metrics["tp"] == 4
    assert invoice_metrics["fp"] == 0
    assert invoice_metrics["fn"] == 1
    assert invoice_metrics["precision"] == 1
    assert invoice_metrics["recall"] == 0.8
