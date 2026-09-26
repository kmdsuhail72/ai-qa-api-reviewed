"""LLM gateway + OpenAI-compatible provider: timeouts, retries, fallback,
error classification, token accounting. Uses httpx.MockTransport - no
network, no real provider."""
import asyncio
import json

import httpx
import pytest

from app.llm.base import (
    LLMBadResponse,
    LLMClientError,
    LLMOverloaded,
    LLMProviderError,
    LLMRateLimited,
    LLMResult,
    LLMTimeout,
)
from app.llm.gateway import LLMGateway
from app.llm.openai_provider import OpenAIProvider


def _ok(model: str, content: str = "hi", p: int = 7, c: int = 3) -> httpx.Response:
    return httpx.Response(200, json={
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c},
    })


def _provider(handler) -> OpenAIProvider:
    return OpenAIProvider(
        api_key="k", base_url="http://llm.test/v1", default_model="primary",
        timeout_seconds=1, max_tokens=16, temperature=0, system_prompt="s",
        transport=httpx.MockTransport(handler),
    )


def _gateway(provider, *, fallback="fallback", retries=3, **kw) -> LLMGateway:
    return LLMGateway(provider, primary_model="primary", fallback_model=fallback,
                      max_retries=retries, backoff_base=0.001, backoff_max=0.002, **kw)


# ---------------- provider ----------------

async def test_provider_parses_answer_and_tokens():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return _ok("primary-2024-07-18", "  42  ", p=11, c=4)

    r = await _provider(handler).complete("q")
    assert r.answer == "42"
    assert (r.prompt_tokens, r.completion_tokens, r.total_tokens) == (11, 4, 15)
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["messages"][-1] == {"role": "user", "content": "q"}


@pytest.mark.parametrize("code,exc", [
    (429, LLMRateLimited), (500, LLMProviderError), (503, LLMProviderError),
    (408, LLMTimeout), (400, LLMClientError), (401, LLMClientError), (404, LLMClientError),
])
async def test_provider_classifies_http_errors(code, exc):
    with pytest.raises(exc):
        await _provider(lambda req: httpx.Response(code, text="x")).complete("q")


async def test_provider_timeout_maps_to_llm_timeout():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)
    with pytest.raises(LLMTimeout):
        await _provider(handler).complete("q")


async def test_provider_rejects_malformed_or_empty_payload():
    with pytest.raises(LLMBadResponse):
        await _provider(lambda r: httpx.Response(200, json={"nope": 1})).complete("q")
    with pytest.raises(LLMBadResponse):
        await _provider(lambda r: httpx.Response(200, json={
            "choices": [{"message": {"content": None}}]})).complete("q")


async def test_provider_reads_retry_after_header():
    with pytest.raises(LLMRateLimited) as ei:
        await _provider(lambda r: httpx.Response(429, headers={"retry-after": "7"})).complete("q")
    assert ei.value.retry_after == 7.0


# ---------------- gateway ----------------

async def test_retries_transient_errors_then_succeeds():
    calls = []

    def handler(req):
        calls.append(json.loads(req.content)["model"])
        return httpx.Response(503) if len(calls) < 3 else _ok("primary")

    r = await _gateway(_provider(handler)).answer("q")
    assert r.answer == "hi" and r.fallback_used is False
    assert calls == ["primary"] * 3


async def test_fallback_used_after_primary_exhausted():
    calls = []

    def handler(req):
        model = json.loads(req.content)["model"]
        calls.append(model)
        return httpx.Response(500) if model == "primary" else _ok("fallback-v2")

    r = await _gateway(_provider(handler), retries=2).answer("q")
    assert r.fallback_used is True
    assert calls == ["primary", "primary", "fallback"]


async def test_versioned_model_name_is_not_mistaken_for_fallback():
    """Regression: OpenAI returns e.g. 'gpt-4o-mini-2024-07-18' for model
    'gpt-4o-mini'; the old code compared names and flagged every answer as
    a fallback."""
    r = await _gateway(_provider(lambda req: _ok("primary-2024-07-18"))).answer("q")
    assert r.fallback_used is False


async def test_client_errors_are_not_retried():
    calls = []

    def handler(req):
        calls.append(json.loads(req.content)["model"])
        return httpx.Response(401, text="bad key")

    with pytest.raises(LLMClientError):
        await _gateway(_provider(handler)).answer("q")
    # one attempt on primary, one on fallback - no retry loop on a 401
    assert calls == ["primary", "fallback"]


async def test_no_fallback_configured_raises_after_retries():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(502)

    with pytest.raises(LLMProviderError):
        await _gateway(_provider(handler), fallback=None, retries=3).answer("q")
    assert len(calls) == 3


class _SlowProvider:
    name = "slow"
    model = "primary"

    def __init__(self, delay: float):
        self.delay = delay

    async def complete(self, question, *, model=None):
        await asyncio.sleep(self.delay)
        return LLMResult(answer="late", model=model or self.model)


async def test_total_budget_turns_into_timeout():
    gw = _gateway(_SlowProvider(0.5), total_timeout=0.05)
    with pytest.raises(LLMTimeout):
        await gw.answer("q")


async def test_concurrency_limit_sheds_load():
    gw = _gateway(_SlowProvider(0.3), fallback=None, max_concurrency=1, queue_timeout=0.02)
    results = await asyncio.gather(gw.answer("a"), gw.answer("b"), return_exceptions=True)
    assert sum(isinstance(r, LLMResult) for r in results) == 1
    assert sum(isinstance(r, LLMOverloaded) for r in results) == 1
