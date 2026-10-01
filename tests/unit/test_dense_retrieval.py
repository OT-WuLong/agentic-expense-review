import json
import os
from datetime import date
from pathlib import Path

import pytest

from app.models import EvidenceSource
from app.retrieval.dense import EMBEDDING_DIMENSION, DensePolicyIndex, EmbeddingUsage


class FakeEmbedder:
    def embed(
        self, texts: list[str], *, text_type: str, timeout_seconds: float | None = None
    ):
        vectors = []
        for text in texts:
            vector = [0.0] * EMBEDDING_DIMENSION
            vector[0 if "交通" in text or "出租车" in text else 1] = 1.0
            vectors.append(vector)
        return vectors, EmbeddingUsage(input_tokens=len(texts), requests=1)


def chunk(
    chunk_id: str,
    *,
    document_id: str,
    text: str,
    department_ids: list[str],
    expense_types: list[str],
    page: int,
) -> dict:
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "source_kind": "POLICY",
        "title": "测试制度",
        "version": "1.0",
        "page_idx": page - 1,
        "page_number": page,
        "title_path": ["测试章节"],
        "char_start": 0,
        "char_end": len(text),
        "text": text,
        "block_indices": [page - 1],
        "bboxes": [],
        "source_sha256": "a" * 64,
        "parse_artifact_sha256": "b" * 64,
        "canonicalizer_version": "1",
        "catalog_snapshot_id": "SNAPSHOT-1",
        "published_status": "PUBLISHED",
        "authority_level": "FORMAL_POLICY",
        "priority": 100,
        "department_ids": department_ids,
        "expense_types": expense_types,
        "effective_from": "2026-01-01",
        "effective_to": None,
        "synthetic": True,
    }


def test_dense_filters_evidence_and_neighbors(tmp_path: Path) -> None:
    rows = [
        chunk(
            "1" * 64,
            document_id="POL-GLOBAL-TRANSPORT",
            text="出租车报销需要业务目的。",
            department_ids=["*"],
            expense_types=["交通"],
            page=1,
        ),
        chunk(
            "2" * 64,
            document_id="POL-GLOBAL-TRANSPORT",
            text="交通票据需要保留原件。",
            department_ids=["*"],
            expense_types=["交通"],
            page=2,
        ),
        chunk(
            "3" * 64,
            document_id="POL-SALES-TRANSPORT",
            text="销售部出租车限额。",
            department_ids=["DEPT-SALES"],
            expense_types=["交通"],
            page=1,
        ),
        chunk(
            "4" * 64,
            document_id="POL-OPS-LODGING",
            text="运营部住宿限额。",
            department_ids=["DEPT-OPERATIONS"],
            expense_types=["住宿"],
            page=1,
        ),
    ]
    chunks_path = tmp_path / "chunks.jsonl"
    chunks_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    index = DensePolicyIndex(
        db_path=Path(f"tmp/milvus/p05-test-{os.getpid()}.db"),
        chunks_path=chunks_path,
        embedder=FakeEmbedder(),
        collection_name=f"test_dense_{os.getpid()}",
    )
    try:
        count, _ = index.rebuild()
        results, _ = index.search(
            "出租车交通报销",
            department_id="DEPT-OPERATIONS",
            expense_type="交通",
            effective_at=date(2026, 5, 1),
            allowed_document_ids=[
                "POL-GLOBAL-TRANSPORT",
                "POL-SALES-TRANSPORT",
                "POL-OPS-LODGING",
            ],
            catalog_snapshot_id="SNAPSHOT-1",
            top_k=5,
        )
        assert count == 4
        assert {item.document_id for item in results} == {"POL-GLOBAL-TRANSPORT"}
        assert all(item.source_type == EvidenceSource.POLICY_DOCUMENT for item in results)
        assert index.search_vector(
            [1.0] + [0.0] * (EMBEDDING_DIMENSION - 1),
            department_id="DEPT-OPERATIONS",
            expense_type="交通",
            effective_at=date(2026, 5, 1),
            allowed_document_ids=[],
            catalog_snapshot_id="SNAPSHOT-1",
        ) == []
        neighbors = index.neighbors(
            document_id="POL-GLOBAL-TRANSPORT",
            anchor_chunk_id="1" * 64,
            department_id="DEPT-OPERATIONS",
            expense_type="交通",
            effective_at=date(2026, 5, 1),
            allowed_document_ids=["POL-GLOBAL-TRANSPORT"],
            catalog_snapshot_id="SNAPSHOT-1",
        )
        assert [item.evidence_id for item in neighbors] == ["2" * 64]
        assert neighbors[0].source_type == EvidenceSource.DOCUMENT_CONTEXT
        with pytest.raises(PermissionError):
            index.neighbors(
                document_id="POL-SALES-TRANSPORT",
                anchor_chunk_id="3" * 64,
                department_id="DEPT-OPERATIONS",
                expense_type="交通",
                effective_at=date(2026, 5, 1),
                allowed_document_ids=["POL-GLOBAL-TRANSPORT"],
                catalog_snapshot_id="SNAPSHOT-1",
            )
    finally:
        index.close()
