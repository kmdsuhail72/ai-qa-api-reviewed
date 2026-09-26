from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class LLMResult:
    answer: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    fallback_used: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMError(Exception):
    """Base class for all LLM-layer failures."""


class LLMTimeout(LLMError):
    """Provider did not respond within the configured timeout. Retryable."""


class LLMRateLimited(LLMError):
    """Provider returned 429 (RPM/TPM/concurrency limit). Retryable."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMProviderError(LLMError):
    """Provider 5xx or transport failure. Retryable."""


class LLMClientError(LLMError):
    """Provider 4xx other than 408/429 (bad key, bad model, bad request).

    NOT retryable on the same model - retrying a 401/400 just burns latency
    and quota. The gateway still tries the fallback model once.
    """


class LLMBadResponse(LLMError):
    """Provider responded 2xx but the payload couldn't be parsed."""


class LLMOverloaded(LLMError):
    """Local concurrency limit reached; request was shed before calling out."""


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str

    async def complete(self, question: str, *, model: str | None = None) -> LLMResult:
        ...
