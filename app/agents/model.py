"""Small OpenAI-compatible client for P07 structured Agent decisions."""

from __future__ import annotations

import json
import os
import time
from typing import TypeVar

import requests
from pydantic import BaseModel

AGENT_MODEL = os.getenv("AGENT_MODEL", "qwen3.8-flash")
ModelOutput = TypeVar("ModelOutput", bound=BaseModel)


def _json_content(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return content.strip()


class StructuredChatClient:
    """Call one configured JSON-mode model and validate the existing contract."""

    model = AGENT_MODEL

    def __init__(self, *, timeout_seconds: float = 45) -> None:
        prefix = "DASHSCOPE" if self.model.startswith("qwen") else "DEEPSEEK"
        self.api_key = os.getenv(f"{prefix}_API_KEY", "")
        self.base_url = os.getenv(f"{prefix}_BASE_URL", "").rstrip("/")
        self.timeout_seconds = timeout_seconds
        if not self.api_key or not self.base_url:
            raise RuntimeError(f"{prefix}_API_KEY and {prefix}_BASE_URL are required")

    def generate(
        self,
        schema: type[ModelOutput],
        *,
        system_prompt: str,
        payload: dict[str, object],
    ) -> tuple[ModelOutput, int, int]:
        """Return the validated object, total tokens, and wall latency in milliseconds."""

        user_prompt = json.dumps(
            {
                "output_schema": schema.model_json_schema(),
                "input": payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        started = time.perf_counter()
        request = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n"
                        "只输出符合 output_schema 的 JSON 对象。不要输出 Markdown、解释或私有思维链；"
                        "reason 字段只写可审计的简短理由。"
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        if self.model.startswith("qwen"):
            request["enable_thinking"] = False
        for attempt in range(3):
            try:
                response = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request,
                    timeout=self.timeout_seconds,
                )
                break
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        if response.status_code != 200:
            raise RuntimeError(f"Agent model request failed with HTTP {response.status_code}")
        body = response.json()
        try:
            content = body["choices"][0]["message"]["content"]
            total_tokens = int(body.get("usage", {}).get("total_tokens", 0))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError("Agent model returned an invalid response envelope") from exc
        return (
            schema.model_validate_json(_json_content(content), context=payload),
            total_tokens,
            latency_ms,
        )
