"""Parse the P04 synthetic corpus with MinerU and build deterministic chunks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ingestion.mineru import MinerUClient, MinerUParseResult, read_mineru_bundle
from app.ingestion.models import ParseStatus
from app.ingestion.pipeline import (
    CANONICALIZER_VERSION,
    canonicalize_and_chunk,
    extract_document_fields,
    preflight_pdf,
)
from app.models import ExtractionStatus

CACHE_DIR = ROOT / "tmp/mineru"
PAGES_PATH = ROOT / "data/fixtures/p04_pages.jsonl"
CHUNKS_PATH = ROOT / "data/fixtures/p04_chunks.jsonl"
REPORT_PATH = ROOT / "evals/reports/p04_extraction.json"
RAW_ROOT = (ROOT / "data/raw").resolve()
ALLOWED_INPUT_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compact(value: object) -> str:
    return re.sub(r"\s+", "", str(value))


def resolve_synthetic_input(document: dict, relative_path: object, declared_sha256: object) -> Path:
    if document.get("synthetic") is not True:
        raise ValueError(f"refusing to upload non-synthetic document: {document.get('document_id')}")
    if document.get("source_type") not in {
        "PROJECT_AUTHORED_SYNTHETIC",
        "REFERENCE_INFORMED_SYNTHETIC",
    }:
        raise ValueError(f"refusing document with unsafe provenance: {document.get('document_id')}")
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("input path must be a non-empty string")
    path = (ROOT / relative_path).resolve(strict=True)
    if not path.is_relative_to(RAW_ROOT) or path.suffix.casefold() not in ALLOWED_INPUT_SUFFIXES:
        raise ValueError(f"input must be a supported file inside data/raw: {relative_path}")
    if not isinstance(declared_sha256, str) or sha256(path) != declared_sha256:
        raise ValueError(f"source SHA-256 mismatch: {document.get('document_id')}")
    return path


def cache_path(path: Path, *, model_version: str, is_ocr: bool) -> Path:
    identity = f"{sha256(path)}:{model_version}:{int(is_ocr)}:table:formula"
    return CACHE_DIR / f"{hashlib.sha256(identity.encode()).hexdigest()}.zip"


def parse_cached(
    path: Path,
    *,
    model_version: str,
    is_ocr: bool,
    client_holder: list[MinerUClient],
) -> tuple[MinerUParseResult, bool]:
    cached = cache_path(path, model_version=model_version, is_ocr=is_ocr)
    if cached.is_file():
        return read_mineru_bundle(cached.read_bytes()), True
    if not client_holder:
        token = os.environ.get("MINERU_TOKEN")
        if not token:
            raise RuntimeError(
                "MINERU_TOKEN is required for uncached files; run with `uv run --env-file .env`"
            )
        client_holder.append(MinerUClient(token))
    result = client_holder[0].parse_file(
        path,
        data_id=f"p04-{sha256(path)[:24]}",
        is_ocr=is_ocr,
        model_version=model_version,
    )
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = cached.with_suffix(".tmp")
    temporary.write_bytes(result.archive)
    temporary.replace(cached)
    return result, False


def read_inputs() -> tuple[list[dict], dict[str, list[dict]], list[str], dict]:
    manifest = json.loads((ROOT / "data/fixtures/source_manifest.json").read_text(encoding="utf-8"))
    scans = json.loads((ROOT / "data/fixtures/p04_scan_manifest.json").read_text(encoding="utf-8"))
    field_gold = json.loads(
        (ROOT / "data/fixtures/p04_field_gold.json").read_text(encoding="utf-8")
    )
    documents = [*manifest["policies"], *manifest["attachments"]]
    expected = {
        item["document_id"]: [
            {"field": field, "value": value} for field, value in item["expected"].items()
        ]
        for item in field_gold["documents"]
    }
    return documents, expected, field_gold["field_universe"], scans


def scan_document(item: dict, source: dict) -> dict:
    return {
        "document_id": item["fixture_id"],
        "title": item["fixture_id"],
        "version": "1",
        "fixture_key": item["fixture_id"],
        "document_type": source["document_type"],
        "media_type": item["media_type"],
        "source_type": "PROJECT_AUTHORED_SYNTHETIC",
        "reference_source_ids": [],
        "license_id": source["license_id"],
        "synthetic": True,
        "source_sha256": item["sha256"],
        "quality_class": item["quality_class"],
    }


def fields_match(actual: list, expected: list[dict]) -> tuple[int, int]:
    by_name = {item.field: item for item in actual}
    matched = 0
    for target in expected:
        field = by_name.get(target["field"])
        if (
            field is not None
            and field.status == ExtractionStatus.PRESENT
            and str(field.value) == str(target["value"])
        ):
            matched += 1
    return matched, len(expected)


def evaluate_fields(
    document_id: str,
    actual: list,
    expected: list[dict],
    field_universe: list[str],
) -> list[dict]:
    by_name = {item.field: item for item in actual}
    expected_by_name = {item["field"]: item["value"] for item in expected}
    rows: list[dict] = []
    for field_name in field_universe:
        field = by_name.get(field_name)
        expected_present = field_name in expected_by_name
        expected_value = expected_by_name.get(field_name)
        predicted_present = field is not None and field.status == ExtractionStatus.PRESENT
        exact_match = bool(
            expected_present
            and predicted_present
            and str(field.value) == str(expected_value)
        )
        rows.append(
            {
                "document_id": document_id,
                "field": field_name,
                "expected_present": expected_present,
                "expected": expected_value,
                "predicted_present": predicted_present,
                "actual": field.value if field is not None else None,
                "status": field.status if field is not None else "MISSING",
                "exact_match": exact_match,
                "tp": exact_match,
                "fp": predicted_present and not exact_match,
                "fn": expected_present and not exact_match,
            }
        )
    return rows


def per_field_metrics(rows: list[dict]) -> dict[str, dict[str, float | int]]:
    metrics: dict[str, dict[str, float | int]] = {}
    for field in sorted({row["field"] for row in rows}):
        selected = [row for row in rows if row["field"] == field]
        tp = sum(row["tp"] for row in selected)
        fp = sum(row["fp"] for row in selected)
        fn = sum(row["fn"] for row in selected)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        metrics[field] = {
            "support": sum(row["expected_present"] for row in selected),
            "negative_support": sum(not row["expected_present"] for row in selected),
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    return metrics


def json_line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def extraction_quality_status(fields: list) -> str:
    if any(field.status == ExtractionStatus.UNREADABLE for field in fields):
        return "LOW_TEXT_QUALITY"
    if any(field.status == ExtractionStatus.MISSING for field in fields):
        return "INCOMPLETE_FIELDS"
    return "PASS"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{json_line(row)}\n" for row in rows), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-golden", action="store_true")
    parser.add_argument("--document-id", action="append", help="ingest only this document")
    parser.add_argument("--cache-only", action="store_true", help="populate MinerU cache only")
    args = parser.parse_args()

    documents, expected_by_document, field_universe, scan_manifest = read_inputs()
    document_by_id = {item["document_id"]: item for item in documents}
    source_scan_document = document_by_id[scan_manifest["source_document_id"]]
    work = [
        (
            item,
            resolve_synthetic_input(item, item.get("pdf_path"), item.get("pdf_sha256")),
            False,
            expected_by_document.get(item["document_id"], []),
        )
        for item in documents
    ]
    for scan in scan_manifest["fixtures"]:
        scan_doc = scan_document(scan, source_scan_document)
        scan_path = resolve_synthetic_input(scan_doc, scan.get("path"), scan.get("sha256"))
        work.append(
            (
                scan_doc,
                scan_path,
                bool(scan["is_ocr"]),
                expected_by_document.get(scan["fixture_id"], []),
            )
        )

    if args.document_id:
        selected = set(args.document_id)
        work = [item for item in work if item[0]["document_id"] in selected]
        missing = selected - {item[0]["document_id"] for item in work}
        if missing:
            raise ValueError(f"unknown document IDs: {sorted(missing)}")

    client_holder: list[MinerUClient] = []
    page_rows: list[dict] = []
    chunk_rows: list[dict] = []
    field_rows: list[dict] = []
    field_evaluation_rows: list[dict] = []
    run_rows: list[dict] = []
    for document, path, forced_ocr, expected_fields in work:
        if not path.is_file():
            raise FileNotFoundError(path)
        is_pdf = path.suffix.casefold() == ".pdf"
        preflight = preflight_pdf(path) if is_pdf else None
        if preflight and preflight.status not in {ParseStatus.READY, ParseStatus.NEEDS_OCR}:
            raise RuntimeError(f"{document['document_id']}: preflight {preflight.status}")
        is_ocr = forced_ocr or bool(preflight and preflight.suggested_ocr)
        model_version = "pipeline"
        result, cache_hit = parse_cached(
            path,
            model_version=model_version,
            is_ocr=is_ocr,
            client_holder=client_holder,
        )
        fields = (
            extract_document_fields(
                result.blocks,
                document,
                quality_flags=[flag for flag in ("OCR" if is_ocr else None,
                                                  document.get("quality_class")) if flag],
            )
            if document.get("document_type")
            else []
        )
        needs_vlm = is_ocr and (
            not result.blocks or any(field.status != ExtractionStatus.PRESENT for field in fields)
        )
        if needs_vlm:
            model_version = "vlm"
            result, cache_hit = parse_cached(
                path,
                model_version=model_version,
                is_ocr=True,
                client_holder=client_holder,
            )
            fields = extract_document_fields(
                result.blocks,
                document,
                quality_flags=[
                    flag
                    for flag in ("OCR", "VLM_FALLBACK", document.get("quality_class"))
                    if flag
                ],
            )
        matched, total = fields_match(fields, expected_fields)

        pages, chunks = canonicalize_and_chunk(
            result.blocks,
            document,
            parse_artifact_sha256=result.archive_sha256,
        )
        page_rows.extend(
            {
                **page.model_dump(mode="json"),
                "document_id": document["document_id"],
                "parse_artifact_sha256": result.archive_sha256,
                "canonicalizer_version": CANONICALIZER_VERSION,
            }
            for page in pages
        )
        chunk_rows.extend(chunk.model_dump(mode="json") for chunk in chunks)
        field_rows.extend(field.model_dump(mode="json") for field in fields)
        field_evaluation_rows.extend(
            evaluate_fields(document["document_id"], fields, expected_fields, field_universe)
            if expected_fields
            else []
        )
        run_rows.append(
            {
                "document_id": document["document_id"],
                "source_sha256": sha256(path),
                "parse_artifact_sha256": result.archive_sha256,
                "mineru_version": result.mineru_version,
                "backend": result.backend or model_version,
                "is_ocr": is_ocr,
                "model_version": model_version,
                "page_count": len(pages),
                "chunk_count": len(chunks),
                "expected_field_matches": matched,
                "expected_field_count": total,
                "quality_status": extraction_quality_status(fields),
            }
        )
        print(
            f"{document['document_id']}: pages={len(pages)} chunks={len(chunks)} "
            f"fields={matched}/{total} model={model_version} cache={cache_hit}"
        )

    if args.cache_only:
        return 0

    evidence_checks: list[dict] = []
    if args.verify_golden:
        golden = json.loads(
            (ROOT / "data/fixtures/golden_cases.json").read_text(encoding="utf-8")
        )
        for case in golden["cases"]:
            for evidence in case["critical_evidence"]:
                if evidence["source_type"] == "STRUCTURED_RECORD":
                    continue
                candidates = [
                    chunk
                    for chunk in chunk_rows
                    if chunk["document_id"] == evidence["document_id"]
                    and chunk["page_number"] == evidence["page"]
                ]
                found = any(compact(evidence["excerpt"]) in compact(chunk["text"])
                            for chunk in candidates)
                evidence_checks.append(
                    {
                        "case_id": case["case_id"],
                        "evidence_id": evidence["evidence_id"],
                        "found": found,
                    }
                )
        missing = [item for item in evidence_checks if not item["found"]]
        if missing:
            raise RuntimeError(f"golden evidence missing from chunks: {missing}")

    if len({row["chunk_id"] for row in chunk_rows}) != len(chunk_rows):
        raise RuntimeError("duplicate chunk IDs")
    page_by_key = {(row["document_id"], row["page_idx"]): row for row in page_rows}
    for chunk in chunk_rows:
        page = page_by_key[(chunk["document_id"], chunk["page_idx"])]
        if page["text"][chunk["char_start"] : chunk["char_end"]] != chunk["text"]:
            raise RuntimeError(f"invalid chunk character range: {chunk['chunk_id']}")

    write_jsonl(PAGES_PATH, page_rows)
    write_jsonl(CHUNKS_PATH, chunk_rows)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    field_total = sum(row["expected_field_count"] for row in run_rows)
    field_matches = sum(row["expected_field_matches"] for row in run_rows)
    field_metrics = per_field_metrics(field_evaluation_rows)
    report = {
        "schema_version": "p04-extraction-report/v1",
        "status": "SMOKE_ONLY",
        "data_classification": "SYNTHETIC",
        "parser": "MinerU precision API",
        "canonicalizer_version": CANONICALIZER_VERSION,
        "documents": run_rows,
        "summary": {
            "document_count": len(run_rows),
            "page_count": len(page_rows),
            "chunk_count": len(chunk_rows),
            "golden_pdf_evidence_found": sum(item["found"] for item in evidence_checks),
            "golden_pdf_evidence_total": len(evidence_checks),
            "field_exact_match_count": field_matches,
            "field_expected_count": field_total,
            "field_macro_f1": (
                sum(item["f1"] for item in field_metrics.values()) / len(field_metrics)
                if field_metrics
                else None
            ),
            "low_text_quality_document_count": sum(
                row["quality_status"] == "LOW_TEXT_QUALITY" for row in run_rows
            ),
            "incomplete_field_document_count": sum(
                row["quality_status"] == "INCOMPLETE_FIELDS" for row in run_rows
            ),
        },
        "golden_evidence": evidence_checks,
        "fields": field_rows,
        "field_evaluations": field_evaluation_rows,
        "field_metrics": field_metrics,
        "notes": [
            "Synthetic smoke evaluation only; this is not a production quality claim.",
            "Field precision/recall/F1 use a complete field universe for labeled documents.",
            "MinerU cloud retention/training/region terms require review before real documents.",
        ],
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {PAGES_PATH.relative_to(ROOT)}, {CHUNKS_PATH.relative_to(ROOT)}")
    print(f"wrote {REPORT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
