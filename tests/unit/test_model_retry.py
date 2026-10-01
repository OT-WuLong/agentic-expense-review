"""Transient TLS failures should not abort a model decision immediately."""

import requests
from pydantic import BaseModel

from app.agents import model as model_module


class _Decision(BaseModel):
    value: str


def test_transient_tls_failure_retries_after_a_pause(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.invalid")
    monkeypatch.setattr(model_module.StructuredChatClient, "model", "deepseek-flash")
    pauses: list[float] = []
    monkeypatch.setattr(model_module.time, "sleep", pauses.append)
    attempts = 0

    class _Response:
        status_code = 200

        def json(self) -> dict:
            return {"choices": [{"message": {"content": '{"value":"ok"}'}}], "usage": {}}

    def post(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise requests.exceptions.SSLError("temporary TLS failure")
        return _Response()

    monkeypatch.setattr(model_module.requests, "post", post)
    result, _, _ = model_module.StructuredChatClient().generate(
        _Decision, system_prompt="test", payload={}
    )
    assert result.value == "ok"
    assert attempts == 3
    assert pauses == [1, 2]
