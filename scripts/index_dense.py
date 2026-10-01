"""Build the P05 local Dense policy index."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.retrieval.dense import (
    COLLECTION_NAME,
    EMBEDDING_DIMENSION,
    PRICE_CNY_PER_1K_INPUT_TOKENS,
    QWEN_MODEL,
    DensePolicyIndex,
    QwenEmbeddingClient,
)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repo_path(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chunks", type=Path, default=ROOT / "data/fixtures/p04_chunks.jsonl"
    )
    parser.add_argument("--db", type=Path, default=ROOT / "data/indexes/policy_dense.db")
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "data/indexes/policy_dense_manifest.json"
    )
    args = parser.parse_args()

    started = time.perf_counter()
    index = DensePolicyIndex(
        db_path=args.db,
        chunks_path=args.chunks,
        embedder=QwenEmbeddingClient(),
    )
    try:
        count, usage = index.rebuild()
    finally:
        index.close()
    manifest = {
        "schema": "policy-dense-index/v1",
        "created_at": datetime.now(UTC).isoformat(),
        "collection": COLLECTION_NAME,
        "database": repo_path(args.db),
        "corpus": repo_path(args.chunks),
        "corpus_sha256": file_sha256(args.chunks),
        "policy_chunks": count,
        "model": QWEN_MODEL,
        "dimension": EMBEDDING_DIMENSION,
        "text_type": "document",
        "output_type": "dense",
        "metric": "COSINE",
        "index_type": "FLAT",
        "usage": {
            "input_tokens": usage.input_tokens,
            "requests": usage.requests,
            "estimated_cost_cny": usage.estimated_cost_cny,
            "price_cny_per_1k_input_tokens": PRICE_CNY_PER_1K_INPUT_TOKENS,
            "price_checked_on": "2026-09-16",
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"indexed {count} policy chunks with {QWEN_MODEL} "
        f"({usage.input_tokens} tokens, {usage.estimated_cost_cny:.10f} CNY)"
    )


if __name__ == "__main__":
    main()
