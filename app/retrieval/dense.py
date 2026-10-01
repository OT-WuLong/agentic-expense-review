"""Minimal Qwen Dense retrieval over a local Milvus Lite policy index."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from pathlib import Path
from typing import Any

import dashscope
from pymilvus import DataType, MilvusClient

from app.ingestion.models import DocumentChunk
from app.models import EvidenceItem, EvidenceSource

QWEN_MODEL = "qwen3.7-text-embedding-flash"
EMBEDDING_DIMENSION = 1024
EMBEDDING_BATCH_SIZE = 20
HYBRID_EMBEDDING_BATCH_SIZE = 3
COLLECTION_NAME = "policy_dense_v1"
PRICE_CNY_PER_1K_INPUT_TOKENS = 0.000125


# 嵌入调用的用量统计：输入 token 数与请求次数，可据此估算成本
@dataclass(frozen=True)
class EmbeddingUsage:
    input_tokens: int = 0
    requests: int = 0

    # 按输入 token 估算调用成本（人民币）
    @property
    def estimated_cost_cny(self) -> float:
        return round(self.input_tokens / 1000 * PRICE_CNY_PER_1K_INPUT_TOKENS, 10)


# DashScope 原生客户端：为查询/文档提供 dense 嵌入（与 Sparse 共用同一入口）
class QwenEmbeddingClient:
    """Small native DashScope client so query/document and Sparse remain available."""

    model = QWEN_MODEL
    dimension = EMBEDDING_DIMENSION

    # 从环境变量读取 API Key 与 base URL，并归一化到原生 /api/v1 端点
    def __init__(self) -> None:
        api_key = os.getenv("DASHSCOPE_API_KEY")
        configured_url = os.getenv("DASHSCOPE_BASE_URL", "").rstrip("/")
        if not api_key or not configured_url:
            raise RuntimeError("DASHSCOPE_API_KEY and DASHSCOPE_BASE_URL are required")
        suffix = "/compatible-mode/v1"
        native_url = (
            f"{configured_url[: -len(suffix)]}/api/v1"
            if configured_url.endswith(suffix)
            else configured_url
        )
        if not native_url.endswith("/api/v1"):
            raise RuntimeError("DASHSCOPE_BASE_URL must end in /api/v1 or /compatible-mode/v1")
        self._api_key = api_key
        # ponytail: one global region is enough for the single-company MVP.
        dashscope.base_http_api_url = native_url

    # 批量嵌入文本（document/query）：自动分批调用并返回向量与用量
    def embed(
        self,
        texts: Sequence[str],
        *,
        text_type: str,
        timeout_seconds: float | None = None,
    ) -> tuple[list[list[float]], EmbeddingUsage]:
        items, usage = self._request(
            texts,
            text_type=text_type,
            output_type="dense",
            timeout_seconds=timeout_seconds,
        )
        vectors = [item["embedding"] for item in items]
        if any(len(vector) != self.dimension for vector in vectors):
            raise RuntimeError("DashScope returned an unexpected dense embedding shape")
        return vectors, usage

    def embed_dense_sparse(
        self,
        texts: Sequence[str],
        *,
        text_type: str,
        timeout_seconds: float | None = None,
    ) -> tuple[list[list[float]], list[dict[int, float]], EmbeddingUsage]:
        items, usage = self._request(
            texts,
            text_type=text_type,
            output_type="dense&sparse",
            timeout_seconds=timeout_seconds,
        )
        dense = [item["embedding"] for item in items]
        sparse = [
            {
                int(component["index"]): float(component["value"])
                for component in item.get("sparse_embedding", [])
            }
            for item in items
        ]
        if any(len(vector) != self.dimension for vector in dense):
            raise RuntimeError("DashScope returned an unexpected dense embedding shape")
        if any(not vector for vector in sparse):
            raise RuntimeError("DashScope returned an empty sparse embedding")
        return dense, sparse, usage

    def embed_sparse(
        self,
        texts: Sequence[str],
        *,
        text_type: str,
        timeout_seconds: float | None = None,
    ) -> tuple[list[dict[int, float]], EmbeddingUsage]:
        items, usage = self._request(
            texts,
            text_type=text_type,
            output_type="sparse",
            timeout_seconds=timeout_seconds,
        )
        sparse = [
            {
                int(component["index"]): float(component["value"])
                for component in item.get("sparse_embedding", [])
            }
            for item in items
        ]
        if any(not vector for vector in sparse):
            raise RuntimeError("DashScope returned an empty sparse embedding")
        return sparse, usage

    def _request(
        self,
        texts: Sequence[str],
        *,
        text_type: str,
        output_type: str,
        timeout_seconds: float | None,
    ) -> tuple[list[dict[str, Any]], EmbeddingUsage]:
        if text_type not in {"document", "query"}:
            raise ValueError("text_type must be document or query")
        items: list[dict[str, Any]] = []
        total_tokens = 0
        request_count = 0
        batch_size = (
            HYBRID_EMBEDDING_BATCH_SIZE
            if "sparse" in output_type
            else EMBEDDING_BATCH_SIZE
        )
        for start in range(0, len(texts), batch_size):
            batch = list(texts[start : start + batch_size])
            response = dashscope.TextEmbedding.call(
                model=self.model,
                input=batch,
                api_key=self._api_key,
                text_type=text_type,
                dimension=self.dimension,
                output_type=output_type,
                **(
                    {"request_timeout": timeout_seconds}
                    if timeout_seconds is not None
                    else {}
                ),
            )
            if response.status_code != HTTPStatus.OK:
                raise RuntimeError(
                    f"DashScope embedding failed: {response.code or response.status_code} "
                    f"{response.message or ''}".strip()
                )
            embeddings: list[dict[str, Any]] = sorted(
                response.output["embeddings"],
                key=lambda item: item.get("text_index", item.get("index", 0)),
            )
            if len(embeddings) != len(batch):
                raise RuntimeError("DashScope returned an unexpected embedding count")
            items.extend(embeddings)
            total_tokens += int((response.usage or {}).get("total_tokens", 0))
            request_count += 1
        return items, EmbeddingUsage(input_tokens=total_tokens, requests=request_count)


# 从 JSONL 读取 chunk，仅保留制度（POLICY）来源
def load_policy_chunks(path: Path) -> list[DocumentChunk]:
    chunks = [
        DocumentChunk.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [chunk for chunk in chunks if chunk.source_kind == "POLICY"]


# 拼接用于嵌入的文本：制度标题 + 章节路径 + 正文
def embedding_text(chunk: DocumentChunk) -> str:
    heading = " > ".join(chunk.title_path)
    body_lines = chunk.text.splitlines()
    heading_lines = {item.strip() for item in [chunk.title, *chunk.title_path] if item}
    while body_lines:
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        if body_lines and body_lines[0].strip() in heading_lines:
            body_lines.pop(0)
            continue
        break
    body = "\n".join(body_lines).strip()
    parts = [f"制度：{chunk.title}" if chunk.title else ""]
    if heading and heading != chunk.title:
        parts.append(f"章节：{heading}")
    if body:
        parts.append(f"正文：{body}")
    return "\n".join(filter(None, parts))


# 把日期编码为 YYYYMMDD 整数，空值按上/下界取 99991231 或 0
def _day(value: date | None, *, upper_bound: bool = False) -> int:
    if value is None:
        return 99991231 if upper_bound else 0
    return value.year * 10000 + value.month * 100 + value.day


# 把字符串转为 Milvus 过滤表达式中的 JSON 字面量
def _literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


# 统一生成 Dense/BM25 共用的服务端过滤表达式，Agent 无法注入原始 filter。
def policy_filter_expression(
    *,
    department_id: str,
    expense_type: str,
    effective_at: date,
    allowed_document_ids: Sequence[str],
    catalog_snapshot_id: str | Sequence[str],
) -> str:
    day = _day(effective_at)
    allowlist = ", ".join(_literal(item) for item in sorted(set(allowed_document_ids)))
    snapshots = (
        f"catalog_snapshot_id == {_literal(catalog_snapshot_id)}"
        if isinstance(catalog_snapshot_id, str)
        else "catalog_snapshot_id in ["
        + ", ".join(_literal(item) for item in sorted(set(catalog_snapshot_id)))
        + "]"
    )
    return " and ".join(
        [
            'source_kind == "POLICY"',
            'published_status == "PUBLISHED"',
            f"document_id in [{allowlist}]",
            (
                "(ARRAY_CONTAINS(department_ids, \"*\") or "
                f"ARRAY_CONTAINS(department_ids, {_literal(department_id)}))"
            ),
            f"ARRAY_CONTAINS(expense_types, {_literal(expense_type)})",
            f"effective_from_day <= {day}",
            f"effective_to_day >= {day}",
            snapshots,
        ]
    )


# 判定 chunk 是否落在授权检索范围内：来源、状态、文档、部门、费用类型、生效期与快照
def _matches_scope(
    chunk: DocumentChunk,
    *,
    department_id: str,
    expense_type: str,
    effective_at: date,
    allowed_document_ids: set[str],
    catalog_snapshot_id: str | Sequence[str],
) -> bool:
    return all(
        (
            chunk.source_kind == "POLICY",
            chunk.published_status == "PUBLISHED",
            chunk.document_id in allowed_document_ids,
            "*" in chunk.department_ids or department_id in chunk.department_ids,
            expense_type in chunk.expense_types,
            chunk.effective_from is not None and chunk.effective_from <= effective_at,
            chunk.effective_to is None or effective_at <= chunk.effective_to,
            chunk.catalog_snapshot_id in (
                [catalog_snapshot_id]
                if isinstance(catalog_snapshot_id, str)
                else catalog_snapshot_id
            ),
        )
    )


# 本地 Milvus Lite 上的 dense 制度索引：建库、重建与带范围过滤的检索
class DensePolicyIndex:
    # 初始化索引：连接 Milvus Lite、加载 chunk 并建立 id 映射，已存在的集合自动加载
    def __init__(
        self,
        *,
        db_path: Path | None = None,
        uri: str | None = None,
        chunks_path: Path,
        embedder: Any,
        collection_name: str = COLLECTION_NAME,
    ) -> None:
        if (db_path is None) == (uri is None):
            raise ValueError("provide exactly one of db_path or uri")
        if db_path is not None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = MilvusClient(uri=str(db_path) if db_path is not None else uri)
        self.collection_name = collection_name
        self.embedder = embedder
        self.chunks = load_policy_chunks(chunks_path)
        self.chunks_by_id = {chunk.chunk_id: chunk for chunk in self.chunks}
        if self.client.has_collection(collection_name=self.collection_name):
            self.client.load_collection(collection_name=self.collection_name)

    # 关闭 Milvus 客户端连接
    def close(self) -> None:
        self.client.close()

    # 重建集合：重算文档向量、重建 schema 与索引并写入，返回行数与用量
    def rebuild(self) -> tuple[int, EmbeddingUsage]:
        vectors, usage = self.embedder.embed(
            [embedding_text(chunk) for chunk in self.chunks], text_type="document"
        )
        if self.client.has_collection(collection_name=self.collection_name):
            self.client.drop_collection(collection_name=self.collection_name)

        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(
            field_name="chunk_id",
            datatype=DataType.VARCHAR,
            is_primary=True,
            max_length=64,
        )
        schema.add_field(
            field_name="dense", datatype=DataType.FLOAT_VECTOR, dim=EMBEDDING_DIMENSION
        )
        schema.add_field(field_name="source_kind", datatype=DataType.VARCHAR, max_length=16)
        schema.add_field(field_name="document_id", datatype=DataType.VARCHAR, max_length=128)
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
        self.client.create_collection(
            collection_name=self.collection_name,
            schema=schema,
            index_params=indexes,
        )
        rows = [
            {
                "chunk_id": chunk.chunk_id,
                "dense": vector,
                "source_kind": chunk.source_kind,
                "document_id": chunk.document_id,
                "department_ids": chunk.department_ids,
                "expense_types": chunk.expense_types,
                "effective_from_day": _day(chunk.effective_from),
                "effective_to_day": _day(chunk.effective_to, upper_bound=True),
                "published_status": chunk.published_status or "",
                "catalog_snapshot_id": chunk.catalog_snapshot_id or "",
            }
            for chunk, vector in zip(self.chunks, vectors, strict=True)
        ]
        if rows:
            self.client.insert(collection_name=self.collection_name, data=rows)
            self.client.flush(collection_name=self.collection_name)
        return len(rows), usage

    # 查询入口：先把查询文本嵌入，再走向量检索
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
    ) -> tuple[list[EvidenceItem], EmbeddingUsage]:
        vectors, usage = self.embedder.embed(
            [query], text_type="query", timeout_seconds=timeout_seconds
        )
        return (
            self.search_vector(
                vectors[0],
                department_id=department_id,
                expense_type=expense_type,
                effective_at=effective_at,
                allowed_document_ids=allowed_document_ids,
                catalog_snapshot_id=catalog_snapshot_id,
                top_k=top_k,
                timeout_seconds=timeout_seconds,
            ),
            usage,
        )

    # 按向量检索：构造范围过滤表达式，按余弦相似度排序并转为可定位证据
    def search_vector(
        self,
        vector: Sequence[float],
        *,
        department_id: str,
        expense_type: str,
        effective_at: date,
        allowed_document_ids: Sequence[str],
        catalog_snapshot_id: str | Sequence[str],
        top_k: int = 5,
        timeout_seconds: float | None = None,
    ) -> list[EvidenceItem]:
        allowed = sorted(set(allowed_document_ids))
        if not allowed:
            return []
        if len(vector) != EMBEDDING_DIMENSION:
            raise ValueError(f"query vector must have {EMBEDDING_DIMENSION} dimensions")
        if top_k < 1:
            raise ValueError("top_k must be positive")
        filter_expression = policy_filter_expression(
            department_id=department_id,
            expense_type=expense_type,
            effective_at=effective_at,
            allowed_document_ids=allowed,
            catalog_snapshot_id=catalog_snapshot_id,
        )
        hits = self.client.search(
            collection_name=self.collection_name,
            data=[list(vector)],
            anns_field="dense",
            filter=filter_expression,
            limit=top_k,
            output_fields=["chunk_id"],
            search_params={"metric_type": "COSINE", "params": {}},
            timeout=timeout_seconds,
        )[0]
        ranked = sorted(
            (
                (str(hit.get("id") or hit["entity"]["chunk_id"]), float(hit["distance"]))
                for hit in hits
            ),
            key=lambda item: (-item[1], item[0]),
        )
        return [self._to_evidence(self.chunks_by_id[chunk_id], score) for chunk_id, score in ranked]

    # 取锚点块的相邻块作为文档上下文证据，并强制校验授权范围
    def neighbors(
        self,
        *,
        document_id: str,
        anchor_chunk_id: str,
        department_id: str,
        expense_type: str,
        effective_at: date,
        allowed_document_ids: Sequence[str],
        catalog_snapshot_id: str | Sequence[str],
        before: int = 1,
        after: int = 1,
    ) -> list[EvidenceItem]:
        if before < 0 or after < 0:
            raise ValueError("neighbor ranges cannot be negative")
        allowed = set(allowed_document_ids)
        if document_id not in allowed:
            raise PermissionError("document context is outside the authorized retrieval scope")
        ordered = sorted(
            (chunk for chunk in self.chunks if chunk.document_id == document_id),
            key=lambda chunk: (chunk.page_idx, chunk.char_start, chunk.chunk_id),
        )
        try:
            anchor = next(i for i, chunk in enumerate(ordered) if chunk.chunk_id == anchor_chunk_id)
        except StopIteration as exc:
            raise KeyError("anchor chunk does not belong to the requested document") from exc
        scope = {
            "department_id": department_id,
            "expense_type": expense_type,
            "effective_at": effective_at,
            "allowed_document_ids": allowed,
            "catalog_snapshot_id": catalog_snapshot_id,
        }
        if not _matches_scope(ordered[anchor], **scope):
            raise PermissionError("document context is outside the authorized retrieval scope")
        selected = ordered[max(0, anchor - before) : anchor] + ordered[
            anchor + 1 : anchor + 1 + after
        ]
        selected = [chunk for chunk in selected if _matches_scope(chunk, **scope)]
        return [self._to_evidence(chunk, None, context=True) for chunk in selected]

    # 把 chunk 转为 EvidenceItem，按是否为上下文选择证据来源类型
    @staticmethod
    def _to_evidence(
        chunk: DocumentChunk,
        score: float | None,
        *,
        context: bool = False,
    ) -> EvidenceItem:
        return EvidenceItem(
            evidence_id=chunk.chunk_id,
            source_type=(
                EvidenceSource.DOCUMENT_CONTEXT if context else EvidenceSource.POLICY_DOCUMENT
            ),
            score=score,
            document_id=chunk.document_id,
            version=chunk.version,
            effective_from=chunk.effective_from,
            effective_to=chunk.effective_to,
            page=chunk.page_number,
            section=" > ".join(chunk.title_path) or None,
            excerpt=chunk.text,
            catalog_snapshot_id=chunk.catalog_snapshot_id,
            published_status=chunk.published_status,
            authority_level=chunk.authority_level,
            priority=chunk.priority,
            supersedes_document_id=chunk.supersedes_document_id,
        )
