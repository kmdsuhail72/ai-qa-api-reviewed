from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration is environment-driven. Secrets have NO defaults."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "ai-qa-api"
    ENV: str = "development"
    SERVICE_VERSION: str = "0.2.0"

    # Security (JWT_SECRET is required - no default)
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60
    JWT_ISSUER: str = "ai-qa-api"
    JWT_AUDIENCE: str = "ai-qa-api-clients"
    BCRYPT_ROUNDS: int = 12

    # Database
    DATABASE_URL: str
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    # Create tables on startup (idempotent). Use Alembic migrations in a real
    # production pipeline; this keeps the demo/compose/k8s stack self-contained.
    DB_AUTO_CREATE: bool = True

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_SOCKET_TIMEOUT_SECONDS: float = 0.5

    # Cache
    CACHE_ENABLED: bool = True
    CACHE_TTL_SECONDS: int = 300
    CACHE_PREFIX: str = "chat:answer:"

    # Rate limiting
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 60
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    RATE_LIMIT_GLOBAL_PER_MINUTE: int = 3000

    # LLM
    LLM_PROVIDER: str = "openai"
    LLM_API_KEY: str
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_FALLBACK_MODEL: str = ""
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_TIMEOUT_SECONDS: float = 20.0
    LLM_MAX_RETRIES: int = 3
    LLM_BACKOFF_BASE_SECONDS: float = 0.5
    LLM_BACKOFF_MAX_SECONDS: float = 4.0
    # Hard ceiling for one /chat call across ALL retries + fallback. Keep it
    # below the ingress/LB read timeout (60s) so clients get a clean 504
    # instead of a proxy-cut connection.
    LLM_TOTAL_TIMEOUT_SECONDS: float = 45.0
    LLM_MAX_TOKENS: int = 1024
    LLM_TEMPERATURE: float = 0.2
    LLM_SYSTEM_PROMPT: str = "You are a concise, helpful assistant."
    # Per-process cap on simultaneous upstream LLM calls (protects provider
    # concurrency limits). Requests wait up to LLM_QUEUE_TIMEOUT_SECONDS for a
    # slot, then get 503 + Retry-After instead of piling up.
    LLM_MAX_CONCURRENCY: int = 50
    LLM_QUEUE_TIMEOUT_SECONDS: float = 5.0
    LLM_HTTP_MAX_CONNECTIONS: int = 100

    # Observability
    METRICS_ENABLED: bool = True
    # Optional static bearer token for Prometheus scraping. Empty = disabled
    # (then only admin JWTs can read /metrics).
    METRICS_TOKEN: str = ""
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"  # json | text

    # Bootstrap users. Seeded only when a password is provided via env.
    # Nothing is hard-coded in source.
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = ""
    ADMIN_EMAIL: str = "admin@example.com"
    DEMO_USER_PASSWORD: str = ""
    DEMO_READONLY_PASSWORD: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
