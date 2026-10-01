"""Milvus Standalone hybrid retrieval with Qwen Dense/Sparse, RRF and reranking."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pymilvus import DataType, Function, FunctionType, MilvusClient
from pymilvus.exceptions import MilvusException
from requests import RequestException

from app.models import EvidenceItem
from app.retrieval.dense import (
    EMBEDDING_DIMENSION,
    DensePolicyIndex,
    EmbeddingUsage,
    _day,
    embedding_text,
    policy_filter_expression,
)
from app.retrieval.reranker import QwenRerankerClient, RerankUsage

COLLECTION_NAME = "policy_hybrid_v1"
RRF_K = 60
DEFAULT_CANDIDATE_K = 20
RetrievalMode = Literal[
    "bm25", "dense", "qwen_sparse", "hybrid_rrf", "hybrid_rerank"
]
_MODES = {"bm25", "dense", "qwen_sparse", "hybrid_rrf", "hybrid_rerank"}


@dataclass(frozen=True)
class HybridSearchUsage:
    embedding: EmbeddingUsage = field(default_factory=EmbeddingUsage)
    rerank: RerankUsage = field(default_factory=RerankUsage)
    degraded_sources: tuple[str, ...] = ()
    dense_candidate_ids: tuple[str, ...] = ()
    sparse_candidate_ids: tuple[str, ...] = ()
    bm25_candidate_ids: tuple[str, ...] = ()

    @property
    def estimated_cost_cny(self) -> float:
        return self.embedding.estimated_cost_cny + self.rerank.estimated_cost_cny


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[tuple[str, float]]], *, k: int = RRF_K
) -> list[tuple[str, float]]:
    if k < 1:
        raise ValueError("RRF k must be positive")
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, (chunk_id, _score) in enumerate(ranking, start=1):
            scores[chunk_id] += 1 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


class HybridPolicyIndex(DensePolicyIndex):
    """Server-backed policy index; the Agent-facing search signature stays unchanged."""

    def __init__(
        self,
        *,
        uri: str,
        chunks_path: Path,
        embedder: Any,
        reranker: QwenRerankerClient | None,
        collection_name: str = COLLECTION_NAME,
        mode: RetrievalMode = "hybrid_rerank",
        candidate_k: int = DEFAULT_CANDIDATE_K,
    ) -> None:
        if mode not in _MODES:
            raise ValueError(f"unsupported retrieval mode: {mode}")
        if candidate_k < 1:
            raise ValueError("candidate_k must be positive")
        super().__init__(
            uri=uri,
            chunks_path=chunks_path,
            embedder=embedder,
            collection_name=collection_name,
        )
        self.chunks = self._deduplicate_documents(self.chunks)
        self.chunks_by_id = {chunk.chunk_id: chunk for chunk in self.chunks}
        self.reranker = reranker
        self.mode = mode
        self.candidate_k = candidate_k

    @staticmethod
    def _deduplicate_documents(chunks: list[Any]) -> list[Any]:
        document_by_hash: dict[str, str] = {}
        for chunk in chunks:
            document_by_hash.setdefault(chunk.source_sha256, chunk.document_id)
        return [
            chunk
            for chunk in chunks
            if document_by_hash[chunk.source_sha256] == chunk.document_id
        ]

    def rebuild(self, *, timeout_seconds: float | None = None) -> tuple[int, EmbeddingUsage]:
        texts = [embedding_text(chunk) for chunk in self.chunks]
        dense, sparse, usage = self.embedder.embed_dense_sparse(
            texts, text_type="document", timeout_seconds=timeout_seconds
        )
        return self.rebuild_from_embeddings(dense, sparse), usage

    def rebuild_from_embeddings(
        self,
        dense: Sequence[Sequence[float]],
        sparse: Sequence[dict[int, float]],
    ) -> int:
        if len(dense) != len(self.chunks) or len(sparse) != len(self.chunks):
            raise ValueError("embedding count must match policy chunk count")
        texts = [embedding_text(chunk) for chunk in self.chunks]
        if self.client.has_collection(collection_name=self.collection_name):
            self.client.drop_collection(collection_name=self.collection_name)

        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(
            field_name="chunk_id", datatype=DataType.VARCHAR, is_primary=True, max_length=64
        )
        schema.add_field(
            field_name="dense", datatype=DataType.FLOAT_VECTOR, dim=EMBEDDING_DIMENSION
        )
        schema.add_field(field_name="qwen_sparse", datatype=DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field(
            field_name="raw_text",
            datatype=DataType.VARCHAR,
            max_length=65535,
            enable_analyzer=True,
            analyzer_params={"type": "chinese"},
        )
        schema.add_field(field_name="bm25_sparse", datatype=DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(
            Function(
                name="policy_bm25",
                function_type=FunctionType.BM25,
                input_field_names=["raw_text"],
                output_field_names=["bm25_sparse"],
            )
        )
        schema.add_field(field_name="source_kind", datatype=DataType.VARCHAR, max_length=16)
        schema.add_field(field_name="document_id", datatype=DataType.VARCHAR, max_length=128)
        schema.add_field(field_name="source_sha256", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(
            field_name="department_ids",
            datatype=DataType.ARRAY,
            element_type=DataType.VARCHAR,
            max_capacity=16,
            max_length=64,
        )
        schema.add_field(
            field_name="expense_types",
            datatype=DataType.ARRAY,
            element_type=DataType.VARCHAR,
            max_capacity=8,
            max_length=32,
        )
        schema.add_field(field_name="effective_from_day", datatype=DataType.INT64)
        schema.add_field(field_name="effective_to_day", datatype=DataType.INT64)
        schema.add_field(
            field_name="published_status", datatype=DataType.VARCHAR, max_length=32
        )
        schema.add_field(
            field_name="catalog_snapshot_id", datatype=DataType.VARCHAR, max_length=128
        )

        indexes = MilvusClient.prepare_index_params()
        indexes.add_index(field_name="dense", index_type="FLAT", metric_type="COSINE")
        indexes.add_index(
            field_name="qwen_sparse",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="IP",
            params={"inverted_index_algo": "DAAT_MAXSCORE"},
        )
        indexes.add_index(
            field_name="bm25_sparse",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
            params={"inverted_index_algo": "DAAT_MAXSCORE"},
        )
        self.client.create_collection(
            collection_name=self.collection_name,
            schema=schema,
            index_params=indexes,
        )
        rows = [
            {
                "chunk_id": chunk.chunk_id,
                "dense": dense_vector,
                "qwen_sparse": sparse_vector,
                "raw_text": text,
                "source_kind": chunk.source_kind,
                "document_id": chunk.document_id,
                "source_sha256": chunk.source_sha256,
                "department_ids": chunk.department_ids,
                "expense_types": chunk.expense_types,
                "effective_from_day": _day(chunk.effective_from),
                "effective_to_day": _day(chunk.effective_to, upper_bound=True),
                "published_status": chunk.published_status or "",
                "catalog_snapshot_id": chunk.catalog_snapshot_id or "",
            }
            for chunk, text, dense_vector, sparse_vector in zip(
                self.chunks, texts, dense, sparse, strict=True
            )
        ]
        if rows:
            self.client.insert(collection_name=self.collection_name, data=rows)
            self.client.flush(collection_name=self.collection_name)
            self.client.load_collection(collection_name=self.collection_name)
        return len(rows)

    def search(
        self,
        query: str,
        *,
        department_id: str,
        expense_type: str,
        effective_at: date,
        allowed_document_ids: Sequence[str],
        catalog_snapshot_id: str | Sequence[str],
        top_k: int = 5,
        timeout_seconds: float | None = None,
    ) -> tuple[list[EvidenceItem], HybridSearchUsage]:
        if not allowed_document_ids:
            return [], HybridSearchUsage()
        if top_k < 1:
            raise ValueError("top_k must be positive")
        filter_expression = policy_filter_expression(
            department_id=department_id,
            expense_type=expense_type,
            effective_at=effective_at,
            allowed_document_ids=allowed_document_ids,
            catalog_snapshot_id=catalog_snapshot_id,
        )
        limit = max(top_k, self.candidate_k)
        rankings: list[list[tuple[str, float]]] = []
        degraded: list[str] = []
        dense_ranking: list[tuple[str, float]] = []
        sparse_ranking: list[tuple[str, float]] = []
        bm25_ranking: list[tuple[str, float]] = []
        embedding_usage = EmbeddingUsage()
        rerank_usage = RerankUsage()

        if self.mode == "bm25":
            bm25_ranking = self._search_route(
                query,
                field="bm25_sparse",
                metric="BM25",
                filter_expression=filter_expression,
                limit=limit,
                timeout_seconds=timeout_seconds,
            )
            rankings.append(bm25_ranking)
        else:
            try:
                if self.mode == "dense":
                    dense, embedding_usage = self._embed_dense_query(
                        query, timeout_seconds=timeout_seconds
                    )
                    sparse = []
                elif self.mode == "qwen_sparse":
                    sparse, embedding_usage = self._embed_sparse_query(
                        query, timeout_seconds=timeout_seconds
                    )
                    dense = []
                else:
                    dense, sparse, embedding_usage = self._embed_query(
                        query, timeout_seconds=timeout_seconds
                    )
            except (RequestException, RuntimeError):
                degraded.extend(
                    [self.mode] if self.mode in {"dense", "qwen_sparse"}
                    else ["dense", "qwen_sparse"]
                )
                bm25_ranking = self._search_route(
                    query,
                    field="bm25_sparse",
                    metric="BM25",
                    filter_expression=filter_expression,
                    limit=limit,
                    timeout_seconds=timeout_seconds,
                )
                rankings.append(bm25_ranking)
            else:
                if self.mode in {"dense", "hybrid_rrf", "hybrid_rerank"}:
                    try:
                        dense_ranking = self._search_route(
                            dense[0],
                            field="dense",
                            metric="COSINE",
                            filter_expression=filter_expression,
                            limit=limit,
                            timeout_seconds=timeout_seconds,
                        )
                        rankings.append(dense_ranking)
                    except MilvusException:
                        if self.mode == "dense":
                            raise
                        degraded.append("dense")
                if self.mode in {"qwen_sparse", "hybrid_rrf", "hybrid_rerank"}:
                    try:
                        sparse_ranking = self._search_route(
                            sparse[0],
                            field="qwen_sparse",
                            metric="IP",
                            filter_expression=filter_expression,
                            limit=limit,
                            timeout_seconds=timeout_seconds,
                        )
                        rankings.append(sparse_ranking)
                    except MilvusException:
                        if self.mode == "qwen_sparse":
                            raise
                        degraded.append("qwen_sparse")

        if not rankings:
            raise RuntimeError("all configured retrieval routes failed")
        fused = (
            reciprocal_rank_fusion(rankings)
            if len(rankings) > 1
            else list(rankings[0])
        )[:limit]

        if self.mode == "hybrid_rerank":
            if self.reranker is None:
                degraded.append("reranker")
            else:
                try:
                    reranked, rerank_usage = self.reranker.rerank(
                        query,
                        [embedding_text(self.chunks_by_id[chunk_id]) for chunk_id, _ in fused],
                        top_n=top_k,
                        timeout_seconds=min(timeout_seconds or 20, 20),
                    )
                    if not reranked:
                        raise RuntimeError("reranker returned no candidates")
                    fused = [(fused[hit.index][0], hit.score) for hit in reranked]
                except (RequestException, RuntimeError):
                    degraded.append("reranker")

        items = [
            self._to_evidence(self.chunks_by_id[chunk_id], score)
            for chunk_id, score in fused[:top_k]
        ]
        return items, HybridSearchUsage(
            embedding=embedding_usage,
            rerank=rerank_usage,
            degraded_sources=tuple(dict.fromkeys(degraded)),
            dense_candidate_ids=tuple(item[0] for item in dense_ranking),
            sparse_candidate_ids=tuple(item[0] for item in sparse_ranking),
            bm25_candidate_ids=tuple(item[0] for item in bm25_ranking),
        )

    def _embed_query(
        self, query: str, *, timeout_seconds: float | None
    ) -> tuple[list[list[float]], list[dict[int, float]], EmbeddingUsage]:
        return self.embedder.embed_dense_sparse(
            [query],
            text_type="query",
            timeout_seconds=min(timeout_seconds or 60, 60),
        )

    def _embed_dense_query(
        self, query: str, *, timeout_seconds: float | None
    ) -> tuple[list[list[float]], EmbeddingUsage]:
        return self.embedder.embed(
            [query],
            text_type="query",
            timeout_seconds=min(timeout_seconds or 60, 60),
        )

    def _embed_sparse_query(
        self, query: str, *, timeout_seconds: float | None
    ) -> tuple[list[dict[int, float]], EmbeddingUsage]:
        return self.embedder.embed_sparse(
            [query],
            text_type="query",
            timeout_seconds=min(timeout_seconds or 60, 60),
        )

    def _search_route(
        self,
        query: str | Sequence[float] | dict[int, float],
        *,
        field: str,
        metric: str,
        filter_expression: str,
        limit: int,
        timeout_seconds: float | None,
    ) -> list[tuple[str, float]]:
        hits = self.client.search(
            collection_name=self.collection_name,
            data=[query],
            anns_field=field,
            filter=filter_expression,
            limit=limit,
            output_fields=["chunk_id"],
            search_params={"metric_type": metric, "params": {}},
            timeout=timeout_seconds,
        )[0]
        return sorted(
            (
                (str(hit.get("id") or hit["entity"]["chunk_id"]), float(hit["distance"]))
                for hit in hits
            ),
            key=lambda item: (-item[1], item[0]),
        )
