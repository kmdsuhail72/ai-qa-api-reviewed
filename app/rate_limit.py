import logging
import time
import uuid

from app.redis_client import redis_client

logger = logging.getLogger("app.rate_limit")

SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local member = ARGV[4]
local window_start = now - window
redis.call('ZREMRANGEBYSCORE', key, 0, window_start)
local count = redis.call('ZCARD', key)
if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local reset_at = window_start + window
    if oldest[2] then
        reset_at = tonumber(oldest[2]) + window
    end
    return {0, count, math.floor(reset_at)}
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, math.ceil(window) + 1)
return {1, count + 1, math.floor(now + window)}
"""


class RateLimitResult:
    __slots__ = ("allowed", "limit", "remaining", "reset_at")

    def __init__(self, allowed, limit, remaining, reset_at):
        self.allowed = allowed
        self.limit = limit
        self.remaining = remaining
        self.reset_at = reset_at


class RateLimiter:
    def __init__(self, client, *, limit, window_seconds, enabled=True):
        self._client = client
        self._limit = limit
        self._window = window_seconds
        self._enabled = enabled
        self._script = client.register_script(SLIDING_WINDOW_LUA)

    def _python_sliding_window(self, key: str, now: float):
        """Pure-Python sliding window fallback for environments without Lua."""
        window_start = now - self._window
        # Remove old entries
        old_members = self._client.zrangebyscore(key, 0, window_start)
        if old_members:
            self._client.zrem(key, *old_members)
        count = self._client.zcard(key)
        if count >= self._limit:
            oldest = self._client.zrange(key, 0, 0, withscores=True)
            reset_at = window_start + self._window
            if oldest:
                reset_at = oldest[0][1] + self._window
            return 0, count, int(reset_at)
        member = f"{now}-{uuid.uuid4().hex}"
        self._client.zadd(key, {member: now})
        self._client.expire(key, int(self._window) + 1)
        return 1, count + 1, int(now + self._window)

    def check(self, identifier: str) -> RateLimitResult:
        if not self._enabled:
            return RateLimitResult(True, self._limit, self._limit, int(time.time()) + self._window)
        key = f"rl:{identifier}"
        now = time.time()
        try:
            allowed, count, reset_at = self._script(
                keys=[key],
                # Unique member passed in (the old version INCR'd a
                # '<key>:seq' counter that never expired -> unbounded key
                # growth, and it touched an undeclared key, which breaks
                # Redis Cluster).
                args=[self._window, self._limit, now, f"{now}-{uuid.uuid4().hex}"],
            )
        except Exception as e:
            # Fall back to a (non-atomic) pure-Python sliding window when the
            # Lua script isn't available (e.g. fakeredis without lupa).
            # If Redis itself is down this also fails -> fail open below.
            try:
                allowed, count, reset_at = self._python_sliding_window(key, now)
            except Exception as e2:
                logger.error(
                    "rate_limit_redis_error",
                    extra={"error": str(e), "fallback_error": str(e2)},
                )
                return RateLimitResult(True, self._limit, self._limit, int(now) + self._window)
        remaining = max(0, self._limit - int(count))
        return RateLimitResult(bool(allowed), self._limit, remaining, int(reset_at))


_user_limiter: RateLimiter | None = None
_global_limiter: RateLimiter | None = None


def get_user_rate_limiter() -> RateLimiter:
    global _user_limiter
    if _user_limiter is None:
        from app.config import get_settings
        s = get_settings()
        _user_limiter = RateLimiter(
            redis_client,
            limit=s.RATE_LIMIT_PER_MINUTE,
            window_seconds=s.RATE_LIMIT_WINDOW_SECONDS,
            enabled=s.RATE_LIMIT_ENABLED,
        )
    return _user_limiter


def get_global_rate_limiter() -> RateLimiter:
    global _global_limiter
    if _global_limiter is None:
        from app.config import get_settings
        s = get_settings()
        _global_limiter = RateLimiter(
            redis_client,
            limit=s.RATE_LIMIT_GLOBAL_PER_MINUTE,
            window_seconds=s.RATE_LIMIT_WINDOW_SECONDS,
            enabled=s.RATE_LIMIT_ENABLED,
        )
    return _global_limiter
