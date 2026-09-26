import logging

import httpx

from app.llm.base import (
    LLMBadResponse,
    LLMClientError,
    LLMProviderError,
    LLMRateLimited,
    LLMResult,
    LLMTimeout,
)

logger = logging.getLogger("app.llm.openai")


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class OpenAIProvider:
    """Minimal OpenAI-compatible chat-completions client.

    Works with: OpenAI, Azure OpenAI (via compatible endpoint), Groq,
    Together, vLLM, Ollama.

    One pooled ``httpx.AsyncClient`` is shared for the life of the process
    (the previous version opened a new client - and a new TCP/TLS
    handshake - on every request).
    """

    name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        default_model: str,
        timeout_seconds: float,
        max_tokens: int,
        temperature: float,
        system_prompt: str,
        max_connections: int = 100,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self.model = default_model
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._system_prompt = system_prompt
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds)),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_connections,
            ),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(self, question: str, *, model: str | None = None) -> LLMResult:
        target_model = model or self.model
        payload = {
            "model": target_model,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": question},
            ],
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._base_url}/chat/completions"

        try:
            resp = await self._client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as e:
            raise LLMTimeout(f"{self.name} timed out after {self._timeout}s") from e
        except httpx.HTTPError as e:
            raise LLMProviderError(f"{self.name} transport error: {type(e).__name__}") from e

        code = resp.status_code
        if code == 429:
            raise LLMRateLimited(
                f"{self.name} rate limited",
                retry_after=_parse_retry_after(resp.headers.get("retry-after")),
            )
        if code == 408:
            raise LLMTimeout(f"{self.name} upstream 408")
        if code >= 500:
            raise LLMProviderError(f"{self.name} {code}: {resp.text[:200]}")
        if code >= 400:
            raise LLMClientError(f"{self.name} {code}: {resp.text[:200]}")

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise LLMBadResponse(f"{self.name} malformed response: {e}") from e
        if not isinstance(content, str) or not content.strip():
            raise LLMBadResponse(f"{self.name} returned empty content")

        return LLMResult(
            answer=content.strip(),
            model=data.get("model") or target_model,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
