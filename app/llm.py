"""Minimal async OpenRouter chat client."""
import httpx

from . import config


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
    if not config.OPENROUTER_KEY:
        raise LLMError("OPENROUTER_KEY is not configured.")
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort, "exclude": True}
    try:
        resp = await client().post(
            config.OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {config.OPENROUTER_KEY}",
                "HTTP-Referer": "http://localhost:8000",
                "X-Title": "Text2SQL Studio",
            },
            json=body,
        )
    except httpx.HTTPError as e:
        raise LLMError(f"Network error contacting OpenRouter: {e}") from e
    if resp.status_code != 200:
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except ValueError:
            detail = resp.text
        raise LLMError(f"OpenRouter error {resp.status_code}: {detail[:300]}")
    data = resp.json()
    if "error" in data:
        raise LLMError(f"OpenRouter error: {data['error'].get('message', data['error'])}")
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"] or ""
    except (KeyError, IndexError) as e:
        raise LLMError("Malformed response from OpenRouter.") from e
    if not content.strip():
        reason = choice.get("finish_reason")
        raise LLMError("The model returned an empty response"
                       + (" (ran out of tokens while reasoning)." if reason == "length" else "."))
    usage = data.get("usage") or {}
    return {
        "content": content,
        "model": data.get("model", model),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "cost": usage.get("cost") or 0.0,
    }
