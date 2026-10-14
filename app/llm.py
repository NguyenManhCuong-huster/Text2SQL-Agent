"""Minimal async client for OpenRouter or any OpenAI-compatible chat-completions endpoint."""
import sqlite3

import httpx

from . import config, history


class LLMError(Exception):
    pass


_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=config.LLM_TIMEOUT_S)
    return _client


async def close() -> None:
    if _client is not None:
        await _client.aclose()


async def chat(messages: list[dict], model: str, temperature: float = 0.0, max_tokens: int = 4000,
               reasoning_effort: str | None = None) -> dict:
    """reasoning_effort ('low'|'medium'|'high') caps hidden thinking on reasoning models; others ignore it."""
    if not config.llm_ready():
        raise LLMError("OPENROUTER_KEY is not configured.")
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    headers = {}
    if config.OPENROUTER_KEY:
        headers["Authorization"] = f"Bearer {config.OPENROUTER_KEY}"
    if config.IS_OPENROUTER:  # OpenRouter-only extensions; other servers may reject unknown fields
        headers.update({"HTTP-Referer": "http://localhost:8000", "X-Title": "Text2SQL Studio"})
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort, "exclude": True}
    try:
        resp = await client().post(config.OPENROUTER_URL, headers=headers, json=body)
    except httpx.HTTPError as e:
        raise LLMError(f"Network error contacting the LLM endpoint: {e}") from e
    if resp.status_code != 200:
        try:
            err = resp.json().get("error", {})
            detail = err.get("message", resp.text) if isinstance(err, dict) else str(err)
        except (ValueError, AttributeError):
            detail = resp.text
        raise LLMError(f"LLM error {resp.status_code}: {str(detail)[:300]}")
    data = resp.json()
    if "error" in data:
        err = data["error"]
        raise LLMError(f"LLM error: {err.get('message', err) if isinstance(err, dict) else err}")
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"] or ""
    except (KeyError, IndexError) as e:
        raise LLMError("Malformed response from the LLM endpoint.") from e
    if not content.strip():
        reason = choice.get("finish_reason")
        raise LLMError("The model returned an empty response"
                       + (" (ran out of tokens while reasoning)." if reason == "length" else "."))
    usage = data.get("usage") or {}
    out = {
        "content": content,
        "model": data.get("model", model),
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "cost": usage.get("cost") or 0.0,
    }
    try:  # every call counts toward DAILY_BUDGET_USD and the usage stats
        history.add_llm_usage(model, out["prompt_tokens"], out["completion_tokens"], out["cost"])
    except sqlite3.Error:
        pass
    return out
