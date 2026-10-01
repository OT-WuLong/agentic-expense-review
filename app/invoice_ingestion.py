"""Turn one uploaded invoice into citable evidence and rule input."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from app.ingestion.mineru import MinerUClient
from app.ingestion.models import ParseStatus
from app.ingestion.pipeline import canonicalize_and_chunk, extract_document_fields, preflight_pdf
from app.models import Document, EvidenceItem, EvidenceSource, ExtractedField


def parse_uploaded_invoice(path: Path, document: Document) -> tuple[list[ExtractedField], list[EvidenceItem]]:
    is_ocr = document.media_type != "application/pdf"
    if not is_ocr:
        preflight = preflight_pdf(path, max_bytes=10 * 1024 * 1024, max_pages=20)
        if preflight.status not in {ParseStatus.READY, ParseStatus.NEEDS_OCR}:
            raise ValueError(f"invoice PDF cannot be parsed: {preflight.status.value}")
        is_ocr = preflight.suggested_ocr

    token = os.getenv("MINERU_TOKEN")
    if not token:
        raise RuntimeError("MINERU_TOKEN is not configured")
    parsed = MinerUClient(token).parse_file(
        path, is_ocr=is_ocr, model_version="vlm" if is_ocr else "pipeline"
    )
    if not any(block.text.strip() for block in parsed.blocks):
        raise ValueError("invoice parser returned no readable text")

    located = extract_document_fields(
        parsed.blocks,
        {"document_id": document.document_id, "document_type": document.document_type},
        quality_flags=["OCR"] if is_ocr else [],
    )
    fields = [
        ExtractedField(
            field=item.field,
            status=item.status,
            value=item.value,
            raw_value=item.raw_value,
            document_id=item.document_id,
            page=item.page,
        )
        for item in located
    ]

    source_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    _, chunks = canonicalize_and_chunk(
        parsed.blocks,
        {
            "document_id": document.document_id,
            "version": "1",
            "title": "上传发票",
            "source_sha256": source_sha256,
            "document_type": document.document_type,
            "media_type": document.media_type,
            "synthetic": False,
        },
        parse_artifact_sha256=parsed.archive_sha256,
    )
    evidence = [
        EvidenceItem(
            evidence_id=chunk.chunk_id,
            source_type=EvidenceSource.ATTACHMENT,
            document_id=document.document_id,
            version="1",
            page=chunk.page_number,
            section=" > ".join(chunk.title_path) or "上传发票",
            excerpt=chunk.text[:1200],
        )
        for chunk in chunks
    ]
    return fields, evidence
