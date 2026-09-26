"""Unit tests for the cache module."""
from app.cache import Cache, cache_key, normalize_question
from app.schemas import ChatResponse


def _sample_response() -> ChatResponse:
    return ChatResponse(
        answer="Berlin.", model="test", prompt_tokens=1, completion_tokens=1,
        latency_ms=5.0, cached=False,
    )


def test_normalize_collapses_whitespace():
    assert normalize_question("  What   is 2+2?  ") == "what is 2+2?"


def test_normalize_lowercases():
    assert normalize_question("WHAT IS 2+2?") == "what is 2+2?"


def test_cache_key_stable_for_equivalent_questions():
    k1 = cache_key("What is 2+2?", prefix="p:")
    k2 = cache_key("  what   IS 2+2? ", prefix="p:")
    assert k1 == k2


def test_cache_key_prefix_scoped():
    assert cache_key("Q", prefix="a:") != cache_key("Q", prefix="b:")


def test_cache_roundtrip(fake_redis):
    cache = Cache(fake_redis, prefix="t:", ttl_seconds=60)
    cache.set_model("Q", _sample_response())
    got = cache.get_model("Q", ChatResponse)
    assert got is not None
    assert got.answer == "Berlin."


def test_cache_miss_returns_none(fake_redis):
    cache = Cache(fake_redis, prefix="t:", ttl_seconds=60)
    assert cache.get_model("never seen", ChatResponse) is None


def test_cache_disabled(fake_redis):
    cache = Cache(fake_redis, prefix="t:", ttl_seconds=60, enabled=False)
    cache.set_model("Q", _sample_response())
    assert cache.get_model("Q", ChatResponse) is None
