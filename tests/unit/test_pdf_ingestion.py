import hashlib
import io
import json
import logging
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfWriter

from app.ingestion.mineru import MinerUProtocolError, _require_https_url, read_mineru_bundle
from app.ingestion.models import ParseStatus
from app.ingestion.pipeline import canonicalize_and_chunk, preflight_pdf
from scripts.ingest_fixtures import resolve_synthetic_input

ROOT = Path(__file__).resolve().parents[2]


def test_frozen_mineru_fixture_builds_traceable_chunks() -> None:
    content = (ROOT / "tests/fixtures/mineru/transport_content_list.json").read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("transport_content_list.json", content)
        archive.writestr("full.md", "synthetic fixture")
        archive.writestr(
            "layout.json", json.dumps({"_version_name": "3.4.4", "_backend": "pipeline"})
        )
    parsed = read_mineru_bundle(buffer.getvalue())
    manifest = json.loads(
        (ROOT / "data/fixtures/source_manifest.json").read_text(encoding="utf-8")
    )
    policy = next(
        item for item in manifest["policies"] if item["document_id"] == "POL-TRANSPORT-2026-V1"
    )
    pages, chunks = canonicalize_and_chunk(
        parsed.blocks,
        policy,
        parse_artifact_sha256=parsed.archive_sha256,
    )
    _, rebuilt_chunks = canonicalize_and_chunk(
        parsed.blocks,
        policy,
        parse_artifact_sha256="0" * 64,
    )

    assert len(pages) == policy["page_count"] == 4
    assert chunks and len({chunk.chunk_id for chunk in chunks}) == len(chunks)
    assert [chunk.chunk_id for chunk in rebuilt_chunks] == [chunk.chunk_id for chunk in chunks]
    assert rebuilt_chunks[0].parse_artifact_sha256 != chunks[0].parse_artifact_sha256
    assert all(
        pages[chunk.page_idx].text[chunk.char_start : chunk.char_end] == chunk.text
        for chunk in chunks
    )
    golden = json.loads(
        (ROOT / "data/fixtures/golden_cases.json").read_text(encoding="utf-8")
    )
    evidence = golden["cases"][0]["critical_evidence"][0]
    assert any(
        chunk.page_number == evidence["page"]
        and "".join(evidence["excerpt"].split()) in "".join(chunk.text.split())
        for chunk in chunks
    )
    frozen_chunks = [
        json.loads(line)
        for line in (ROOT / "data/fixtures/p04_chunks.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any("城市等级 | 每间夜上限 | 核算单位" in row["text"] for row in frozen_chunks)


def test_pdf_preflight_returns_explicit_boundary_statuses(tmp_path) -> None:
    source = ROOT / "data/raw/policies/POL-TRANSPORT-2026-V1.pdf"
    assert preflight_pdf(source).status == ParseStatus.READY
    scan = ROOT / "data/raw/scans/DOC-GC-A-TAXI-001-low-quality.pdf"
    assert preflight_pdf(scan).status == ParseStatus.NEEDS_OCR
    assert preflight_pdf(source, max_bytes=1).status == ParseStatus.TOO_LARGE

    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert preflight_pdf(empty).status == ParseStatus.EMPTY

    encrypted = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    with encrypted.open("wb") as output:
        writer.write(output)
    result = preflight_pdf(encrypted)
    assert result.status == ParseStatus.ENCRYPTED
    assert result.file_sha256 == hashlib.sha256(encrypted.read_bytes()).hexdigest()


def test_cloud_upload_boundary_rejects_untrusted_paths_and_hosts() -> None:
    assert logging.getLogger("httpx2").level >= logging.WARNING
    assert logging.getLogger("httpcore2").level >= logging.WARNING
    document = {
        "document_id": "unsafe",
        "synthetic": True,
        "source_type": "PROJECT_AUTHORED_SYNTHETIC",
    }
    with pytest.raises(ValueError, match="inside data/raw"):
        resolve_synthetic_input(document, "README.md", "0" * 64)
    with pytest.raises(MinerUProtocolError, match="invalid upload URL"):
        _require_https_url(
            "https://127.0.0.1/upload?signature=secret",
            "upload",
            frozenset({"mineru.oss-cn-shanghai.aliyuncs.com"}),
        )
