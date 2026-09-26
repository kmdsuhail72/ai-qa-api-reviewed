import redis

from app.config import get_settings

settings = get_settings()

# Short socket timeouts: Redis is an accelerator (cache + rate limit), so a
# slow/unavailable Redis must degrade quickly instead of hanging requests.
redis_client = redis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    socket_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
    socket_connect_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
    health_check_interval=30,
)


def get_redis() -> redis.Redis:
    return redis_client


def check_redis() -> bool:
    try:
        return bool(redis_client.ping())
    except Exception:
        return False
