"""Qwen text reranker client used after hybrid recall."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from http import HTTPStatus

import dashscope

QWEN_RERANK_MODEL = "qwen3.7-text-rerank"
RERANK_PRICE_CNY_PER_1K_INPUT_TOKENS = 0.0005
RERANK_INSTRUCT = (
    "Given an enterprise expense-policy query, rank passages by how directly they "
    "answer the query under the stated policy scope."
)


@dataclass(frozen=True)
class RerankHit:
    index: int
    score: float


@dataclass(frozen=True)
class RerankUsage:
    input_tokens: int = 0
    requests: int = 0

    @property
    def estimated_cost_cny(self) -> float:
        return round(self.input_tokens / 1000 * RERANK_PRICE_CNY_PER_1K_INPUT_TOKENS, 10)


class QwenRerankerClient:
    """Thin DashScope adapter for qwen3.7-text-rerank."""

    model = QWEN_RERANK_MODEL

    def __init__(self) -> None:
        api_key = os.getenv("DASHSCOPE_API_KEY")
        configured_url = os.getenv("DASHSCOPE_RERANK_BASE_URL") or os.getenv(
            "DASHSCOPE_BASE_URL", ""
        )
        configured_url = configured_url.rstrip("/")
        if not api_key or not configured_url:
            raise RuntimeError("DASHSCOPE_API_KEY and a DashScope base URL are required")
        suffix = "/compatible-mode/v1"
        self._base_url = (
            f"{configured_url[: -len(suffix)]}/api/v1"
            if configured_url.endswith(suffix)
            else configured_url
        )
        if not self._base_url.endswith("/api/v1"):
            raise RuntimeError("DashScope rerank base URL must end in /api/v1")
        self._api_key = api_key

    def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        top_n: int,
        timeout_seconds: float | None = None,
    ) -> tuple[list[RerankHit], RerankUsage]:
        if not documents:
            return [], RerankUsage()
        if top_n < 1:
            raise ValueError("top_n must be positive")
        dashscope.base_http_api_url = self._base_url
        response = dashscope.TextReRank.call(
            model=self.model,
            query=query,
            documents=list(documents),
            top_n=min(top_n, len(documents)),
            instruct=RERANK_INSTRUCT,
            api_key=self._api_key,
            **(
                {"request_timeout": timeout_seconds}
                if timeout_seconds is not None
                else {}
            ),
        )
        if response.status_code != HTTPStatus.OK:
            raise RuntimeError(
                f"DashScope rerank failed: {response.code or response.status_code} "
                f"{response.message or ''}".strip()
            )
        hits = [
            RerankHit(index=int(item["index"]), score=float(item["relevance_score"]))
            for item in (response.output or {}).get("results", [])
        ]
        if any(hit.index < 0 or hit.index >= len(documents) for hit in hits):
            raise RuntimeError("DashScope rerank returned an invalid document index")
        usage = response.usage or {}
        return hits, RerankUsage(
            input_tokens=int(usage.get("total_tokens", usage.get("prompt_tokens", 0))),
            requests=1,
        )
