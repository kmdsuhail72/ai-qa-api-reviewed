from app.config import get_settings
from app.llm.base import LLMProvider
from app.llm.gateway import LLMGateway
from app.llm.openai_provider import OpenAIProvider

OPENAI_COMPATIBLE = {"openai", "azure-openai", "groq", "together", "vllm", "ollama"}


def build_provider(provider_name: str | None = None) -> LLMProvider:
    settings = get_settings()
    name = (provider_name or settings.LLM_PROVIDER).lower()
    if name in OPENAI_COMPATIBLE:
        return OpenAIProvider(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
            default_model=settings.LLM_MODEL,
            timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
            max_tokens=settings.LLM_MAX_TOKENS,
            temperature=settings.LLM_TEMPERATURE,
            system_prompt=settings.LLM_SYSTEM_PROMPT,
            max_connections=settings.LLM_HTTP_MAX_CONNECTIONS,
        )
    raise ValueError(f"Unsupported LLM provider: {name}")


def build_gateway() -> LLMGateway:
    settings = get_settings()
    return LLMGateway(
        build_provider(),
        primary_model=settings.LLM_MODEL,
        fallback_model=settings.LLM_FALLBACK_MODEL or None,
        max_retries=settings.LLM_MAX_RETRIES,
        backoff_base=settings.LLM_BACKOFF_BASE_SECONDS,
        backoff_max=settings.LLM_BACKOFF_MAX_SECONDS,
        max_concurrency=settings.LLM_MAX_CONCURRENCY,
        queue_timeout=settings.LLM_QUEUE_TIMEOUT_SECONDS,
        total_timeout=settings.LLM_TOTAL_TIMEOUT_SECONDS,
    )
