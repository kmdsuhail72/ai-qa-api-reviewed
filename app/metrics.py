from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)

# --- HTTP layer ---
REQUEST_COUNT = Counter(
    "api_requests_total",
    "Total HTTP requests.",
    ["method", "endpoint", "status"],
)

REQUEST_LATENCY = Histogram(
    "api_request_latency_seconds",
    "HTTP request latency in seconds.",
    ["method", "endpoint"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40),
)

IN_FLIGHT = Gauge(
    "api_in_flight_requests",
    "Number of in-flight HTTP requests.",
)

# --- LLM layer ---
LLM_CALLS = Counter(
    "llm_calls_total",
    "Upstream LLM call attempts by outcome (success | error | shed).",
    ["provider", "model", "outcome"],
)

LLM_LATENCY = Histogram(
    "llm_latency_seconds",
    "LLM call latency in seconds.",
    ["provider", "model"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40),
)

LLM_TOKENS = Counter(
    "llm_tokens_total",
    "LLM token usage.",
    ["model", "type"],
)

LLM_IN_FLIGHT = Gauge(
    "llm_in_flight_requests",
    "Upstream LLM calls currently in progress (this process).",
)

LLM_RETRIES = Counter(
    "llm_retries_total",
    "LLM retry attempts.",
    ["provider", "model"],
)

LLM_FALLBACKS = Counter(
    "llm_fallback_total",
    "LLM fallback invocations.",
    ["provider", "model"],
)

# --- Cache / rate limit ---
CACHE_HITS = Counter("chat_cache_hits_total", "Chat cache hits.")
CACHE_MISSES = Counter("chat_cache_misses_total", "Chat cache misses.")
CACHE_WRITES = Counter("chat_cache_writes_total", "Chat cache writes.")
RATE_LIMITED = Counter(
    "rate_limited_total",
    "Requests rejected by rate limiter.",
    ["scope"],
)

CHAT_REQUESTS = Counter(
    "chat_requests_total",
    "Terminal outcome of /chat requests.",
    ["outcome"],  # success | cache_hit | llm_timeout | llm_rate_limited | llm_overloaded | llm_error
)

# --- DB ---
CHAT_LOGS_WRITTEN = Counter(
    "chat_logs_written_total",
    "Chat log rows written.",
    ["status"],
)

# --- App info ---
APP_INFO = Info("app", "Application metadata.")


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
