"""RBAC matrix, admin/report endpoints, /health, /metrics and HTTP error mapping."""
import pytest

from app.llm.base import LLMOverloaded, LLMRateLimited, LLMResult, LLMTimeout
from tests.conftest import FakeGateway, auth_header


# ---------- RBAC ----------

def test_readonly_cannot_chat(client, readonly_token, fake_gateway_factory):
    fake = fake_gateway_factory(FakeGateway())
    r = client.post("/chat", json={"question": "hi"}, headers=auth_header(readonly_token))
    assert r.status_code == 403
    assert fake.calls == []


def test_admin_can_chat(client, admin_token, fake_gateway_factory):
    fake_gateway_factory(FakeGateway())
    r = client.post("/chat", json={"question": "hi"}, headers=auth_header(admin_token))
    assert r.status_code == 200


def test_readonly_can_read_usage_report(client, readonly_token, user_token, fake_gateway_factory):
    fake_gateway_factory(FakeGateway(result=LLMResult(answer="a", model="m1",
                                                      prompt_tokens=10, completion_tokens=5)))
    client.post("/chat", json={"question": "one"}, headers=auth_header(user_token))
    client.post("/chat", json={"question": "one"}, headers=auth_header(user_token))  # cache hit

    r = client.get("/reports/usage", headers=auth_header(readonly_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_requests"] == 2
    assert body["successful_requests"] == 2
    assert body["prompt_tokens"] == 10 and body["completion_tokens"] == 5  # cache hit = 0 new tokens
    assert body["by_model"][0]["model"] == "m1"


def test_user_cannot_read_reports_or_admin(client, user_token):
    assert client.get("/reports/usage", headers=auth_header(user_token)).status_code == 403
    assert client.get("/admin/users", headers=auth_header(user_token)).status_code == 403


def test_readonly_cannot_manage_users(client, readonly_token):
    r = client.post("/admin/users", headers=auth_header(readonly_token),
                    json={"username": "eve", "password": "password123"})
    assert r.status_code == 403


def test_admin_user_lifecycle(client, admin_token):
    h = auth_header(admin_token)
    r = client.post("/admin/users", headers=h,
                    json={"username": "alice", "password": "s3cret-pass", "role": "user"})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    assert client.post("/admin/users", headers=h,
                       json={"username": "alice", "password": "s3cret-pass"}).status_code == 409

    login = client.post("/auth/login", json={"username": "alice", "password": "s3cret-pass"})
    assert login.status_code == 200

    r = client.patch(f"/admin/users/{uid}", headers=h, json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    # disabled user's existing token is rejected
    me = client.get("/auth/me", headers=auth_header(login.json()["access_token"]))
    assert me.status_code == 403


def test_admin_cannot_disable_self(client, admin_token):
    h = auth_header(admin_token)
    me = client.get("/auth/me", headers=h).json()
    r = client.patch(f"/admin/users/{me['id']}", headers=h, json={"is_active": False})
    assert r.status_code == 400


# ---------- /health ----------

@pytest.mark.parametrize("db,redis,code,status", [
    (True, True, 200, "ok"),
    (True, False, 200, "degraded"),
    (False, True, 503, "down"),
])
def test_health(client, monkeypatch, db, redis, code, status):
    import app.main
    monkeypatch.setattr(app.main, "check_db", lambda: db)
    monkeypatch.setattr(app.main, "check_redis", lambda: redis)
    r = client.get("/health")
    assert r.status_code == code
    body = r.json()
    assert body["status"] == status
    assert body["database"] == ("ok" if db else "error")
    assert body["redis"] == ("ok" if redis else "error")
    assert body["version"]


# ---------- /metrics ----------

def test_metrics_scrape_token(client):
    r = client.get("/metrics", headers=auth_header("test-scrape-token"))
    assert r.status_code == 200
    assert "api_requests_total" in r.text


def test_metrics_rejects_anonymous_and_bad_token(client):
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers=auth_header("wrong")).status_code == 401


def test_request_latency_and_tokens_are_recorded(client, user_token, fake_gateway_factory):
    fake_gateway_factory(FakeGateway(result=LLMResult(answer="x", model="tok-model",
                                                      prompt_tokens=21, completion_tokens=9)))
    r = client.post("/chat", json={"question": "metrics please"}, headers=auth_header(user_token))
    assert r.status_code == 200
    assert r.json()["total_tokens"] == 30
    assert "X-Response-Time-ms" in r.headers

    text = client.get("/metrics", headers=auth_header("test-scrape-token")).text
    assert 'api_request_latency_seconds_count{endpoint="/chat",method="POST"}' in text
    assert 'api_requests_total{endpoint="/chat",method="POST",status="200"}' in text
    assert 'chat_requests_total{outcome="success"}' in text


# ---------- HTTP error mapping ----------

@pytest.mark.parametrize("exc,code", [
    (LLMTimeout("t"), 504),
    (LLMRateLimited("r", retry_after=12), 503),
    (LLMOverloaded("o"), 503),
])
def test_llm_errors_map_to_http_codes(client, user_token, fake_gateway_factory, exc, code):
    fake_gateway_factory(FakeGateway(raises=exc))
    r = client.post("/chat", json={"question": "boom"}, headers=auth_header(user_token))
    assert r.status_code == code
    assert "Retry-After" in r.headers


def test_rate_limit_returns_429_with_headers(client, user_token, fake_gateway_factory, fake_redis):
    from app.main import app
    from app.rate_limit import RateLimiter, get_user_rate_limiter

    fake_gateway_factory(FakeGateway())
    app.dependency_overrides[get_user_rate_limiter] = lambda: RateLimiter(
        fake_redis, limit=1, window_seconds=60)
    h = auth_header(user_token)
    assert client.post("/chat", json={"question": "a"}, headers=h).status_code == 200
    r = client.post("/chat", json={"question": "b"}, headers=h)
    assert r.status_code == 429
    assert r.headers["X-RateLimit-Remaining"] == "0"
    assert int(r.headers["Retry-After"]) >= 1


def test_blank_question_rejected(client, user_token):
    r = client.post("/chat", json={"question": "   "}, headers=auth_header(user_token))
    assert r.status_code == 422
