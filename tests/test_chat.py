"""Tests for /chat with a mocked LLM gateway."""

from app.llm.base import LLMProviderError, LLMResult
from tests.conftest import auth_header


def test_readiness_reports_dependency_status(client, monkeypatch):
    import app.main

    monkeypatch.setattr(app.main, "check_db", lambda: True)
    monkeypatch.setattr(app.main, "check_redis", lambda: True)
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": True, "redis": True},
    }


def test_readiness_returns_service_unavailable_when_database_is_down(client, monkeypatch):
    import app.main

    monkeypatch.setattr(app.main, "check_db", lambda: False)
    monkeypatch.setattr(app.main, "check_redis", lambda: True)
    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": False, "redis": True},
    }


def test_readiness_stays_ready_but_degraded_when_redis_is_down(client, monkeypatch):
    """Redis is an accelerator; losing it must not pull every pod out of the LB."""
    import app.main

    monkeypatch.setattr(app.main, "check_db", lambda: True)
    monkeypatch.setattr(app.main, "check_redis", lambda: False)
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


def test_chat_requires_auth(client):
    r = client.post("/chat", json={"question": "hi"})
    assert r.status_code == 401


def test_chat_empty_question(client, user_token):
    r = client.post(
        "/chat",
        json={"question": ""},
        headers=auth_header(user_token),
    )
    assert r.status_code == 422


def test_chat_success(client, user_token, fake_gateway_factory, fake_rate_limiters, fake_cache):
    from tests.conftest import FakeGateway
    fake_gateway_factory(FakeGateway(result=LLMResult(
        answer="The answer is 42.",
        model="test-model",
        prompt_tokens=12,
        completion_tokens=8,
    )))

    r = client.post(
        "/chat",
        json={"question": "What is the meaning of life?"},
        headers=auth_header(user_token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "The answer is 42."
    assert body["model"] == "test-model"
    assert body["prompt_tokens"] == 12
    assert body["completion_tokens"] == 8
    assert body["cached"] is False


def test_chat_persists_to_db_and_history(client, user_token, fake_gateway_factory, fake_rate_limiters, fake_cache):
    from tests.conftest import FakeGateway
    fake_gateway_factory(FakeGateway(result=LLMResult(
        answer="Berlin.", model="test-model", prompt_tokens=5, completion_tokens=2,
    )))

    headers = auth_header(user_token)
    r1 = client.post("/chat", json={"question": "What is the capital of Germany?"}, headers=headers)
    assert r1.status_code == 200

    r2 = client.get("/chat/history", headers=headers)
    assert r2.status_code == 200
    rows = r2.json()
    assert len(rows) == 1
    assert rows[0]["question"] == "What is the capital of Germany?"
    assert rows[0]["answer"] == "Berlin."


def test_chat_returns_502_on_llm_error(client, user_token, fake_gateway_factory, fake_rate_limiters, fake_cache):
    from tests.conftest import FakeGateway
    fake_gateway_factory(FakeGateway(raises=LLMProviderError("provider down")))

    r = client.post(
        "/chat",
        json={"question": "will fail"},
        headers=auth_header(user_token),
    )
    assert r.status_code == 502
    assert "unavailable" in r.json()["detail"].lower()


def test_chat_cache_hit_on_second_call(client, user_token, fake_gateway_factory, fake_rate_limiters, fake_cache):
    from tests.conftest import FakeGateway
    fake = fake_gateway_factory(FakeGateway())

    headers = auth_header(user_token)
    r1 = client.post("/chat", json={"question": "same question"}, headers=headers)
    r2 = client.post("/chat", json={"question": "same question"}, headers=headers)

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["cached"] is False
    assert r2.json()["cached"] is True
    assert fake.calls == ["same question"]  # LLM called only once


def test_rate_limiter_blocks_over_limit(fake_redis):
    """Direct unit test: 2 allowed, 3rd blocked."""
    from app.rate_limit import RateLimiter

    limiter = RateLimiter(fake_redis, limit=2, window_seconds=60, enabled=True)

    r1 = limiter.check("user:test")
    r2 = limiter.check("user:test")
    r3 = limiter.check("user:test")

    assert r1.allowed is True
    assert r2.allowed is True
    assert r3.allowed is False
    assert r3.remaining == 0
    assert r3.reset_at > 0


def test_rate_limiter_isolates_users(fake_redis):
    from app.rate_limit import RateLimiter

    limiter = RateLimiter(fake_redis, limit=1, window_seconds=60, enabled=True)

    assert limiter.check("user:a").allowed is True
    assert limiter.check("user:a").allowed is False
    assert limiter.check("user:b").allowed is True
