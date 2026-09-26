import hashlib
import logging
from typing import Optional, TypeVar

from pydantic import BaseModel, ValidationError

from app.redis_client import redis_client

logger = logging.getLogger("app.cache")

T = TypeVar("T", bound=BaseModel)


def normalize_question(question: str) -> str:
    return " ".join(question.strip().lower().split())


def cache_key(question: str, *, prefix: str) -> str:
    h = hashlib.sha256(normalize_question(question).encode("utf-8")).hexdigest()
    return f"{prefix}{h}"


class Cache:
    def __init__(self, client, *, prefix: str, ttl_seconds: int, enabled: bool = True):
        self._client = client
        self._prefix = prefix
        self._ttl = ttl_seconds
        self._enabled = enabled

    def get_model(self, question: str, model_cls: type[T]) -> Optional[T]:
        if not self._enabled:
            return None
        key = cache_key(question, prefix=self._prefix)
        try:
            raw = self._client.get(key)
        except Exception as e:
            logger.warning("cache_get_failed", extra={"error": str(e)})
            return None
        if raw is None:
            return None
        try:
            return model_cls.model_validate_json(raw)
        except ValidationError:
            logger.warning("cache_invalid_entry", extra={"key": key})
            try:
                self._client.delete(key)
            except Exception:
                pass
            return None

    def set_model(self, question: str, value: BaseModel) -> None:
        if not self._enabled:
            return
        key = cache_key(question, prefix=self._prefix)
        try:
            self._client.set(key, value.model_dump_json(), ex=self._ttl)
        except Exception as e:
            logger.warning("cache_set_failed", extra={"error": str(e)})


_cache: Cache | None = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        from app.config import get_settings

        s = get_settings()
        _cache = Cache(
            redis_client,
            prefix=s.CACHE_PREFIX,
            ttl_seconds=s.CACHE_TTL_SECONDS,
            enabled=s.CACHE_ENABLED,
        )
    return _cache
