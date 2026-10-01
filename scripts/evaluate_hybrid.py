"""Compare BM25, Qwen Sparse, Dense, RRF and qwen3.7 reranking."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.retrieval.dense import QwenEmbeddingClient, load_policy_chunks
from app.retrieval.hybrid import HybridPolicyIndex, RetrievalMode
from app.retrieval.reranker import QWEN_RERANK_MODEL, QwenRerankerClient
from scripts.evaluate_dense import (
    eligible,
    evidence_chunk_map,
    load_jsonl,
    ndcg,
    nearest_rank,
)

MODES: tuple[RetrievalMode, ...] = (
    "bm25",
    "qwen_sparse",
    "dense",
    "hybrid_rrf",
    "hybrid_rerank",
)


def docker_stats() -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "stats", "agentic-approval-milvus", "--no-stream", "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode or not result.stdout.strip():
        return {"status": "UNAVAILABLE"}
    return {"status": "MEASURED", **json.loads(result.stdout.strip())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chunks", type=Path, default=ROOT / "data/fixtures/p04_chunks.jsonl"
    )
    parser.add_argument(
        "--dataset", type=Path, default=ROOT / "evals/datasets/retrieval.jsonl"
    )
    parser.add_argument(
        "--reference", type=Path, default=ROOT / "evals/datasets/reference_evidence.json"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "evals/reports/hybrid_ablation.json"
    )
    parser.add_argument("--uri", default=os.getenv("MILVUS_URI", "http://localhost:19530"))
    parser.add_argument("--split", default="validation", choices=("dev", "validation", "test"))
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=MODES)
    args = parser.parse_args()

    samples = [
        item for item in load_jsonl(args.dataset) if item["split"] == args.split
    ]
    chunks = load_policy_chunks(args.chunks)
    chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    evidence_map = evidence_chunk_map(args.reference, chunks)
    allowed_documents = sorted(
        {chunk.document_id for chunk in chunks if chunk.published_status == "PUBLISHED"}
    )
    allowed_set = set(allowed_documents)
    configurations: list[dict[str, Any]] = []

    for mode in args.modes:
        index = HybridPolicyIndex(
            uri=args.uri,
            chunks_path=args.chunks,
            embedder=QwenEmbeddingClient(),
            reranker=QwenRerankerClient() if mode == "hybrid_rerank" else None,
            mode=mode,
        )
        recall_values: list[float] = []
        ndcg_values: list[float] = []
        latencies: list[float] = []
        degraded_queries = 0
        filter_violations = 0
        embedding_tokens = 0
        embedding_requests = 0
        rerank_tokens = 0
        rerank_requests = 0
        sample_results: list[dict[str, Any]] = []
        try:
            for sample in samples:
                filters = sample["filters"]
                started = time.perf_counter()
                candidates, usage = index.search(
                    sample["query"],
                    department_id=filters["department_id"],
                    expense_type=filters["expense_type"],
                    effective_at=date.fromisoformat(filters["effective_at"]),
                    allowed_document_ids=allowed_documents,
                    catalog_snapshot_id=sample["policy_catalog_snapshot_id"],
                    top_k=args.top_k,
                    timeout_seconds=90,
                )
                latency_ms = round((time.perf_counter() - started) * 1000, 3)
                candidate_ids = [item.evidence_id for item in candidates]
                groups = sample["relevant_evidence_groups"]
                gold_groups = [
                    {evidence_map[item] for item in group["any_of"] if item in evidence_map}
                    for group in groups
                ]
                if groups:
                    recall_values.append(
                        sum(bool(set(candidate_ids[:5]) & group) for group in gold_groups)
                        / len(gold_groups)
                    )
                    ndcg_values.append(ndcg(candidate_ids, groups, evidence_map))
                violations = [
                    chunk_id
                    for chunk_id in candidate_ids
                    if not eligible(chunks_by_id[chunk_id], sample, allowed_set)
                ]
                filter_violations += len(violations)
                latencies.append(latency_ms)
                degraded_queries += bool(usage.degraded_sources)
                embedding_tokens += usage.embedding.input_tokens
                embedding_requests += usage.embedding.requests
                rerank_tokens += usage.rerank.input_tokens
                rerank_requests += usage.rerank.requests
                sample_results.append(
                    {
                        "sample_id": sample["sample_id"],
                        "latency_ms": latency_ms,
                        "candidate_ids": candidate_ids,
                        "degraded_sources": list(usage.degraded_sources),
                        "dense_candidates": list(usage.dense_candidate_ids),
                        "sparse_candidates": list(usage.sparse_candidate_ids),
                        "bm25_candidates": list(usage.bm25_candidate_ids),
                        "filter_violations": violations,
                    }
                )
        finally:
            index.close()
        configurations.append(
            {
                "mode": mode,
                "recall_at_5": sum(recall_values) / len(recall_values),
                "ndcg_at_10": sum(ndcg_values) / len(ndcg_values),
                "p95_latency_ms": nearest_rank(latencies, 0.95),
                "filter_violations": filter_violations,
                "degraded_queries": degraded_queries,
                "usage": {
                    "embedding_input_tokens": embedding_tokens,
                    "embedding_requests": embedding_requests,
                    "rerank_input_tokens": rerank_tokens,
                    "rerank_requests": rerank_requests,
                },
                "samples": sample_results,
            }
        )

    report = {
        "schema": "hybrid-retrieval-ablation/v1",
        "status": "MEASURED",
        "generated_at": datetime.now(UTC).isoformat(),
        "split": args.split,
        "sample_count": len(samples),
        "top_k": args.top_k,
        "rrf_k": 60,
        "embedding_model": "qwen3.7-text-embedding-flash",
        "reranker_model": QWEN_RERANK_MODEL,
        "configurations": configurations,
        "milvus_resource_snapshot": docker_stats(),
        "notes": [
            "Qwen Dense and Qwen Sparse are stored and queried as separate candidate routes.",
            "BM25 is an ablation baseline and network-independent fallback, not the primary sparse route.",
            "A reranker failure preserves RRF order and is counted as a degraded query.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                item["mode"]: {
                    "recall@5": item["recall_at_5"],
                    "ndcg@10": item["ndcg_at_10"],
                    "p95_ms": item["p95_latency_ms"],
                    "degraded": item["degraded_queries"],
                }
                for item in configurations
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
