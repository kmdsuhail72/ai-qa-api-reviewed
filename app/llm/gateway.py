"""LLM gateway: timeout budget, bounded concurrency, retry w/ backoff, fallback.

Request flow for one /chat call::

    answer()
      └─ overall deadline (LLM_TOTAL_TIMEOUT_SECONDS, < ingress read timeout)
           ├─ primary model: up to LLM_MAX_RETRIES attempts
           │     each attempt: acquire concurrency slot -> provider call
           │     retry only on timeout / 429 / 5xx / transport errors
           │     backoff = min(base * 2^n, max) with full jitter,
           │               or the provider's Retry-After if larger (capped)
           └─ fallback model (if configured & different): same loop
"""
import asyncio
import logging
import random
import time

from app.llm.base import (
    LLMError,
    LLMOverloaded,
    LLMProvider,
    LLMProviderError,
    LLMRateLimited,
    LLMResult,
    LLMTimeout,
)
from app.metrics import (
    LLM_CALLS,
    LLM_FALLBACKS,
    LLM_IN_FLIGHT,
    LLM_LATENCY,
    LLM_RETRIES,
    LLM_TOKENS,
)

logger = logging.getLogger("app.llm.gateway")

RETRYABLE = (LLMTimeout, LLMRateLimited, LLMProviderError)


class LLMGateway:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        primary_model: str,
        fallback_model: str | None,
        max_retries: int,
        backoff_base: float,
        backoff_max: float,
        max_concurrency: int = 50,
        queue_timeout: float = 5.0,
        total_timeout: float | None = None,
    ) -> None:
        self._provider = provider
        self.primary_model = primary_model
        self.fallback_model = fallback_model or None
        self._max_retries = max(1, max_retries)
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._sem = asyncio.Semaphore(max(1, max_concurrency))
        self._queue_timeout = queue_timeout
        self._total_timeout = total_timeout

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    def _backoff(self, attempt: int, exc: Exception) -> float:
        delay = min(self._backoff_base * (2 ** (attempt - 1)), self._backoff_max)
        delay = random.uniform(delay / 2, delay)  # jitter: avoid thundering herd
        retry_after = getattr(exc, "retry_after", None)
        if retry_after:
            delay = max(delay, min(float(retry_after), self._backoff_max))
        return delay

    async def _call_once(self, question: str, model: str) -> LLMResult:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self._queue_timeout)
        except asyncio.TimeoutError as e:
            LLM_CALLS.labels(provider=self._provider.name, model=model, outcome="shed").inc()
            raise LLMOverloaded("LLM concurrency limit reached") from e

        LLM_IN_FLIGHT.inc()
        started = time.perf_counter()
        try:
            result = await self._provider.complete(question, model=model)
        except LLMError:
            LLM_CALLS.labels(provider=self._provider.name, model=model, outcome="error").inc()
            raise
        finally:
            LLM_LATENCY.labels(provider=self._provider.name, model=model).observe(
                time.perf_counter() - started
            )
            LLM_IN_FLIGHT.dec()
            self._sem.release()

        LLM_CALLS.labels(provider=self._provider.name, model=model, outcome="success").inc()
        LLM_TOKENS.labels(model=model, type="prompt").inc(result.prompt_tokens)
        LLM_TOKENS.labels(model=model, type="completion").inc(result.completion_tokens)
        return result

    async def _attempt(self, question: str, model: str) -> LLMResult:
        last_exc: LLMError | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await self._call_once(question, model)
            except RETRYABLE as e:
                last_exc = e
                if attempt == self._max_retries:
                    break
                LLM_RETRIES.labels(provider=self._provider.name, model=model).inc()
                delay = self._backoff(attempt, e)
                logger.warning(
                    "llm_retry",
                    extra={"model": model, "attempt": attempt,
                           "error": type(e).__name__, "delay_s": round(delay, 3)},
                )
                await asyncio.sleep(delay)
            # Non-retryable (client error, bad response, overloaded): fail fast.
        assert last_exc is not None
        raise last_exc

    async def _answer(self, question: str) -> LLMResult:
        try:
            return await self._attempt(question, self.primary_model)
        except LLMOverloaded:
            raise  # we are the bottleneck; fallback would just queue again
        except LLMError as primary_exc:
            logger.warning(
                "llm_primary_failed",
                extra={"model": self.primary_model, "error": type(primary_exc).__name__},
            )
            if not self.fallback_model or self.fallback_model == self.primary_model:
                raise
            LLM_FALLBACKS.labels(provider=self._provider.name, model=self.fallback_model).inc()
            try:
                result = await self._attempt(question, self.fallback_model)
            except LLMError as fallback_exc:
                logger.error(
                    "llm_fallback_failed",
                    extra={"model": self.fallback_model, "error": type(fallback_exc).__name__},
                )
                raise fallback_exc from primary_exc
            result.fallback_used = True
            return result

    async def answer(self, question: str) -> LLMResult:
        if self._total_timeout is None:
            return await self._answer(question)
        try:
            return await asyncio.wait_for(self._answer(question), timeout=self._total_timeout)
        except asyncio.TimeoutError as e:
            raise LLMTimeout(f"LLM budget of {self._total_timeout}s exhausted") from e

    async def aclose(self) -> None:
        close = getattr(self._provider, "aclose", None)
        if close is not None:
            await close()
