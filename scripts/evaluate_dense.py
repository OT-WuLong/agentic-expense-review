"""Run the frozen single-pass P05 Dense retrieval baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ingestion.models import DocumentChunk
from app.retrieval.dense import (
    COLLECTION_NAME,
    EMBEDDING_DIMENSION,
    PRICE_CNY_PER_1K_INPUT_TOKENS,
    QWEN_MODEL,
    DensePolicyIndex,
    QwenEmbeddingClient,
    load_policy_chunks,
)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repo_path(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def compact(text: str) -> str:
    return "".join(text.split())


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def evidence_chunk_map(
    reference_path: Path, chunks: list[DocumentChunk]
) -> dict[str, str]:
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for item in reference["items"]:
        if item["source_type"] != "POLICY_DOCUMENT":
            continue
        needle = compact(item["excerpt"])
        matches = [
            chunk.chunk_id
            for chunk in chunks
            if chunk.document_id == item["document_id"]
            and chunk.page_number == item["page"]
            and needle in compact(chunk.text)
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"{item['evidence_id']} maps to {len(matches)} chunks; expected exactly one"
            )
        mapping[item["evidence_id"]] = matches[0]
    return mapping


def eligible(chunk: DocumentChunk, sample: dict[str, Any], allowed: set[str]) -> bool:
    filters = sample["filters"]
    effective_at = date.fromisoformat(filters["effective_at"])
    return all(
        (
            chunk.document_id in allowed,
            chunk.source_kind == "POLICY",
            chunk.published_status == "PUBLISHED",
            "*" in chunk.department_ids or filters["department_id"] in chunk.department_ids,
            filters["expense_type"] in chunk.expense_types,
            chunk.effective_from is not None and chunk.effective_from <= effective_at,
            chunk.effective_to is None or effective_at <= chunk.effective_to,
            chunk.catalog_snapshot_id == sample["policy_catalog_snapshot_id"],
        )
    )


def metric(
    metric_id: str,
    values: list[float],
    *,
    direction: str,
    run_id: str,
    split: str,
) -> dict[str, Any]:
    if not values:
        return {
            "metric_id": metric_id,
            "status": "NOT_APPLICABLE",
            "value": None,
            "numerator": None,
            "denominator": 0,
            "support": 0,
            "unit": "ratio",
            "direction": direction,
            "split": split,
            "slice": "overall",
            "run_id": run_id,
        }
    numerator = sum(values)
    return {
        "metric_id": metric_id,
        "status": "MEASURED",
        "value": numerator / len(values),
        "numerator": numerator,
        "denominator": len(values),
        "support": len(values),
        "unit": "ratio",
        "direction": direction,
        "split": split,
        "slice": "overall",
        "run_id": run_id,
    }


def ndcg(
    candidate_ids: list[str], groups: list[dict[str, Any]], evidence_map: dict[str, str]
) -> float:
    group_chunks = [
        ({evidence_map[item] for item in group["any_of"] if item in evidence_map}, group["relevance"])
        for group in groups
    ]
    seen_groups: set[int] = set()
    dcg = 0.0
    for rank, chunk_id in enumerate(candidate_ids[:10], start=1):
        matched = [
            index
            for index, (chunk_ids, _) in enumerate(group_chunks)
            if index not in seen_groups and chunk_id in chunk_ids
        ]
        if not matched:
            continue
        relevance = max(group_chunks[index][1] for index in matched)
        seen_groups.update(matched)
        dcg += (2**relevance - 1) / math.log2(rank + 1)

    # Multiple gold evidence groups may intentionally resolve to one atomic chunk.
    relevance_by_chunk: dict[str, int] = {}
    for chunk_ids, relevance in group_chunks:
        for chunk_id in chunk_ids:
            relevance_by_chunk[chunk_id] = max(relevance_by_chunk.get(chunk_id, 0), relevance)
    ideal = sorted(relevance_by_chunk.values(), reverse=True)[:10]
    idcg = sum((2**relevance - 1) / math.log2(rank + 1) for rank, relevance in enumerate(ideal, 1))
    return dcg / idcg if idcg else 0.0


def git_state() -> dict[str, Any]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    return {"commit": commit, "dirty": dirty}


def nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(percentile * len(ordered)) - 1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chunks", type=Path, default=ROOT / "data/fixtures/p04_chunks.jsonl"
    )
    parser.add_argument("--db", type=Path, default=ROOT / "data/indexes/policy_dense.db")
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "data/indexes/policy_dense_manifest.json"
    )
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/datasets/retrieval.jsonl")
    parser.add_argument(
        "--reference", type=Path, default=ROOT / "evals/datasets/reference_evidence.json"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "evals/reports/baseline_dense.json"
    )
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--split", choices=("dev", "validation", "test"), default="validation")
    args = parser.parse_args()

    index_manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    corpus_hash = file_sha256(args.chunks)
    chunks = load_policy_chunks(args.chunks)
    expected_manifest = {
        "collection": COLLECTION_NAME,
        "corpus_sha256": corpus_hash,
        "policy_chunks": len(chunks),
        "model": QWEN_MODEL,
        "dimension": EMBEDDING_DIMENSION,
        "text_type": "document",
        "output_type": "dense",
        "metric": "COSINE",
        "index_type": "FLAT",
    }
    mismatches = {
        key: {"expected": value, "actual": index_manifest.get(key)}
        for key, value in expected_manifest.items()
        if index_manifest.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"Dense index manifest mismatch; rebuild required: {mismatches}")
    samples = [
        sample for sample in load_jsonl(args.dataset) if sample["split"] == args.split
    ]
    if not samples:
        raise RuntimeError(f"retrieval dataset has no {args.split} samples")
    chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    evidence_map = evidence_chunk_map(args.reference, chunks)
    allowed_documents = sorted(
        {chunk.document_id for chunk in chunks if chunk.published_status == "PUBLISHED"}
    )
    allowed_set = set(allowed_documents)
    run_material = json.dumps(
        {
            "corpus": corpus_hash,
            "dataset": file_sha256(args.dataset),
            "reference": file_sha256(args.reference),
            "model": QWEN_MODEL,
            "dimension": EMBEDDING_DIMENSION,
            "top_k": args.top_k,
            "split": args.split,
        },
        sort_keys=True,
    ).encode()
    run_id = f"P05-DENSE-{hashlib.sha256(run_material).hexdigest()[:12]}"

    embedder = QwenEmbeddingClient()
    index = DensePolicyIndex(db_path=args.db, chunks_path=args.chunks, embedder=embedder)
    recall_values: list[float] = []
    mrr_values: list[float] = []
    ndcg_values: list[float] = []
    filter_violations = 0
    candidate_count = 0
    results: list[dict[str, Any]] = []
    try:
        row_count = int(index.client.get_collection_stats(COLLECTION_NAME)["row_count"])
        if row_count != index_manifest["policy_chunks"]:
            raise RuntimeError(
                f"Dense collection row count is {row_count}; "
                f"expected {index_manifest['policy_chunks']}"
            )
        embedding_started = time.perf_counter()
        query_vectors, query_usage = embedder.embed(
            [sample["query"] for sample in samples], text_type="query"
        )
        query_embedding_batch_latency_ms = round(
            (time.perf_counter() - embedding_started) * 1000, 3
        )
        for sample, vector in zip(samples, query_vectors, strict=True):
            started = time.perf_counter()
            filters = sample["filters"]
            candidates = index.search_vector(
                vector,
                department_id=filters["department_id"],
                expense_type=filters["expense_type"],
                effective_at=date.fromisoformat(filters["effective_at"]),
                allowed_document_ids=allowed_documents,
                catalog_snapshot_id=sample["policy_catalog_snapshot_id"],
                top_k=args.top_k,
            )
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            candidate_ids = [item.evidence_id for item in candidates]
            groups = sample["relevant_evidence_groups"]
            group_chunks = [
                {evidence_map[item] for item in group["any_of"] if item in evidence_map}
                for group in groups
            ]
            matched_by_candidate = [
                [
                    group["group_id"]
                    for group, gold_chunks in zip(groups, group_chunks, strict=True)
                    if chunk_id in gold_chunks
                ]
                for chunk_id in candidate_ids
            ]
            violations = [
                chunk_id
                for chunk_id in candidate_ids
                if not eligible(chunks_by_id[chunk_id], sample, allowed_set)
            ]
            filter_violations += len(violations)
            candidate_count += len(candidate_ids)

            errors: list[dict[str, str]] = []
            if not sample["no_answer"]:
                hits_at_5 = sum(bool(set(candidate_ids[:5]) & gold) for gold in group_chunks)
                recall_values.append(hits_at_5 / len(group_chunks))
                first_rank = next(
                    (
                        rank
                        for rank, chunk_id in enumerate(candidate_ids, start=1)
                        if any(chunk_id in gold for gold in group_chunks)
                    ),
                    None,
                )
                mrr_values.append(1 / first_rank if first_rank else 0.0)
                ndcg_values.append(ndcg(candidate_ids, groups, evidence_map))
                for group, gold in zip(groups, group_chunks, strict=True):
                    if set(candidate_ids[:5]) & gold:
                        continue
                    if not gold:
                        category = "chunking_error"
                    elif not any(eligible(chunks_by_id[item], sample, allowed_set) for item in gold):
                        category = "filtering_error"
                    elif set(candidate_ids) & gold:
                        category = "ranking_error"
                    else:
                        category = "missed_recall"
                    errors.append({"group_id": group["group_id"], "category": category})

            results.append(
                {
                    "sample_id": sample["sample_id"],
                    "split": sample["split"],
                    "query": sample["query"],
                    "filters": filters,
                    "catalog_snapshot_id": sample["policy_catalog_snapshot_id"],
                    "no_answer": sample["no_answer"],
                    "vector_search_latency_ms": latency_ms,
                    "filter_violations": violations,
                    "errors": errors,
                    "candidates": [
                        {
                            "rank": rank,
                            "matched_groups": matched_by_candidate[rank - 1],
                            "evidence": evidence.model_dump(mode="json"),
                        }
                        for rank, evidence in enumerate(candidates, start=1)
                    ],
                }
            )
    finally:
        index.close()

    metrics = [
        metric(
            "retrieval.recall_at_5",
            recall_values,
            direction="higher",
            run_id=run_id,
            split=args.split,
        ),
        metric(
            "retrieval.mrr",
            mrr_values,
            direction="higher",
            run_id=run_id,
            split=args.split,
        ),
        metric(
            "retrieval.ndcg_at_10",
            ndcg_values,
            direction="higher",
            run_id=run_id,
            split=args.split,
        ),
        {
            "metric_id": "retrieval.no_answer_accuracy",
            "status": (
                "NOT_MEASURED"
                if any(sample["no_answer"] for sample in samples)
                else "NOT_APPLICABLE"
            ),
            "value": None,
            "numerator": None,
            "denominator": sum(sample["no_answer"] for sample in samples),
            "support": sum(sample["no_answer"] for sample in samples),
            "unit": "ratio",
            "direction": "higher",
            "split": args.split,
            "slice": "overall",
            "run_id": run_id,
        },
        {
            "metric_id": "retrieval.filter_violation_rate",
            "status": "MEASURED" if candidate_count else "NOT_APPLICABLE",
            "value": filter_violations / candidate_count if candidate_count else None,
            "numerator": filter_violations,
            "denominator": candidate_count,
            "support": candidate_count,
            "unit": "ratio",
            "direction": "lower",
            "split": args.split,
            "slice": "overall",
            "run_id": run_id,
        },
    ]
    report = {
        "schema": "dense-retrieval-report/v1",
        "status": "MEASURED",
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "git": git_state(),
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "concurrency": 1,
            "cache": "Milvus index warm; query embeddings generated once as one batch",
        },
        "config": {
            "model": QWEN_MODEL,
            "dimension": EMBEDDING_DIMENSION,
            "document_text_type": "document",
            "query_text_type": "query",
            "output_type": "dense",
            "metric": "COSINE",
            "index_type": "FLAT",
            "top_k": args.top_k,
            "split": args.split,
            "retrieval_calls_per_question": 1,
            "query_rewrites": 0,
            "authorization": "server-built document allowlist",
            "filters": [
                "catalog_snapshot_id",
                "published_status",
                "department_ids",
                "expense_types",
                "effective date range",
                "document allowlist",
            ],
        },
        "artifacts": {
            "corpus": repo_path(args.chunks),
            "corpus_sha256": corpus_hash,
            "dataset": repo_path(args.dataset),
            "dataset_sha256": file_sha256(args.dataset),
            "reference": repo_path(args.reference),
            "reference_sha256": file_sha256(args.reference),
        },
        "usage": {
            "index": index_manifest["usage"],
            "queries": {
                "input_tokens": query_usage.input_tokens,
                "requests": query_usage.requests,
                "estimated_cost_cny": query_usage.estimated_cost_cny,
                "price_cny_per_1k_input_tokens": PRICE_CNY_PER_1K_INPUT_TOKENS,
                "price_checked_on": "2026-09-16",
            },
        },
        "latency": {
            "query_embedding_batch_ms": query_embedding_batch_latency_ms,
            "query_embedding_batch_size": len(samples),
            "vector_search_raw_ms": [item["vector_search_latency_ms"] for item in results],
            "vector_search_p50_ms": nearest_rank(
                [item["vector_search_latency_ms"] for item in results], 0.5
            ),
            "vector_search_p95_ms": nearest_rank(
                [item["vector_search_latency_ms"] for item in results], 0.95
            ),
        },
        "metrics": metrics,
        "samples": results,
        "notes": [
            "The validation split has no no-answer sample; no refusal threshold is calibrated.",
            "Evaluator-only EVID-* labels are mapped to runtime chunk IDs outside the retriever.",
            "nDCG ideal ranking folds evidence groups that resolve to the same atomic chunk.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = {item["metric_id"]: item["value"] for item in metrics}
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
