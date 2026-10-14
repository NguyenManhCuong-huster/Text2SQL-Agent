import asyncio

import httpx
import pytest

from app import config, llm


class _Capture:
    def __init__(self):
        self.request = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 1}})


@pytest.fixture
def capture(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(llm, "_client", httpx.AsyncClient(transport=httpx.MockTransport(cap.handler)))
    return cap


def test_openrouter_sends_reasoning_and_key(capture):
    out = asyncio.run(llm.chat([{"role": "user", "content": "x"}], "m", reasoning_effort="low"))
    assert out["content"] == "hi" and out["prompt_tokens"] == 3
    body = capture.request.read().decode()
    assert '"reasoning"' in body and capture.request.headers["authorization"] == "Bearer test-key"


def test_local_endpoint_without_key(capture, monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_URL", "http://localhost:11434/v1/chat/completions")
    monkeypatch.setattr(config, "IS_OPENROUTER", False)
    monkeypatch.setattr(config, "OPENROUTER_KEY", "")
    assert config.llm_ready()
    asyncio.run(llm.chat([{"role": "user", "content": "x"}], "qwen", reasoning_effort="low"))
    assert "authorization" not in capture.request.headers
    assert '"reasoning"' not in capture.request.read().decode()
    assert str(capture.request.url).startswith("http://localhost:11434")


def test_missing_key_for_openrouter(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_KEY", "")
    with pytest.raises(llm.LLMError):
        asyncio.run(llm.chat([], "m"))


def test_error_payload_is_reported(monkeypatch):
    def handler(_):
        return httpx.Response(402, json={"error": {"message": "insufficient credits"}})
    monkeypatch.setattr(llm, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(llm.LLMError, match="insufficient credits"):
        asyncio.run(llm.chat([{"role": "user", "content": "x"}], "m"))
