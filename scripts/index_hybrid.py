"""Rebuild the Milvus Standalone Qwen Dense/Sparse + BM25 policy index."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.retrieval.dense import (
    EMBEDDING_DIMENSION,
    HYBRID_EMBEDDING_BATCH_SIZE,
    QWEN_MODEL,
    QwenEmbeddingClient,
    embedding_text,
)
from app.retrieval.hybrid import COLLECTION_NAME, HybridPolicyIndex


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chunks", type=Path, default=ROOT / "data/fixtures/p04_chunks.jsonl"
    )
    parser.add_argument("--uri", default=os.getenv("MILVUS_URI", "http://localhost:19530"))
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "data/indexes/policy_hybrid_manifest.json"
    )
    parser.add_argument(
        "--cache", type=Path, default=ROOT / "data/indexes/policy_hybrid_vectors.json"
    )
    args = parser.parse_args()

    index = HybridPolicyIndex(
        uri=args.uri,
        chunks_path=args.chunks,
        embedder=QwenEmbeddingClient(),
        reranker=None,
        mode="hybrid_rrf",
    )
    started = time.perf_counter()
    cache = (
        json.loads(args.cache.read_text(encoding="utf-8"))
        if args.cache.exists()
        else {}
    )
    if cache.get("model") != QWEN_MODEL or cache.get("dimension") != EMBEDDING_DIMENSION:
        cache = {
            "model": QWEN_MODEL,
            "dimension": EMBEDDING_DIMENSION,
            "input_tokens": 0,
            "requests": 0,
            "entries": {},
        }
    entries = cache["entries"]
    missing = [
        chunk
        for chunk in index.chunks
        if chunk.chunk_id not in entries
        or entries[chunk.chunk_id]["source_sha256"] != chunk.source_sha256
    ]
    reused = len(index.chunks) - len(missing)
    try:
        for start in range(0, len(missing), HYBRID_EMBEDDING_BATCH_SIZE):
            batch = missing[start : start + HYBRID_EMBEDDING_BATCH_SIZE]
            dense, sparse, usage = index.embedder.embed_dense_sparse(
                [embedding_text(chunk) for chunk in batch],
                text_type="document",
                timeout_seconds=90,
            )
            for chunk, dense_vector, sparse_vector in zip(
                batch, dense, sparse, strict=True
            ):
                entries[chunk.chunk_id] = {
                    "source_sha256": chunk.source_sha256,
                    "dense": dense_vector,
                    "sparse": sparse_vector,
                }
            cache["input_tokens"] += usage.input_tokens
            cache["requests"] += usage.requests
            args.cache.parent.mkdir(parents=True, exist_ok=True)
            args.cache.write_text(
                json.dumps(cache, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(
                f"embedded {min(start + len(batch), len(missing))}/{len(missing)} missing chunks",
                flush=True,
            )
        count = index.rebuild_from_embeddings(
            [entries[chunk.chunk_id]["dense"] for chunk in index.chunks],
            [
                {int(key): float(value) for key, value in entries[chunk.chunk_id]["sparse"].items()}
                for chunk in index.chunks
            ],
        )
        document_hashes = sorted({chunk.source_sha256 for chunk in index.chunks})
    finally:
        index.close()

    manifest = {
        "schema": "policy-hybrid-index/v1",
        "created_at": datetime.now(UTC).isoformat(),
        "uri": args.uri,
        "collection": COLLECTION_NAME,
        "policy_chunks": count,
        "deduplicated_documents": len(document_hashes),
        "cached_chunks_reused": reused,
        "embedding_model": QWEN_MODEL,
        "dimension": EMBEDDING_DIMENSION,
        "dense": {"field": "dense", "metric": "COSINE", "index_type": "FLAT"},
        "qwen_sparse": {
            "field": "qwen_sparse",
            "metric": "IP",
            "index_type": "SPARSE_INVERTED_INDEX",
        },
        "bm25": {
            "field": "bm25_sparse",
            "analyzer": "chinese",
            "index_type": "SPARSE_INVERTED_INDEX",
        },
        "usage": {
            "input_tokens": cache["input_tokens"],
            "requests": cache["requests"],
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"collection": COLLECTION_NAME, "rows": count}, ensure_ascii=False))


if __name__ == "__main__":
    main()
