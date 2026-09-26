"""Tests for the /health and /health/live endpoints."""


def test_liveness(client):
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "alive"}


def test_root(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["app"] == "ai-qa-api"


def test_frontend(client):
    r = client.get("/ui/")
    assert r.status_code == 200
    assert "Orbit" in r.text


def test_request_id_header_generated(client):
    r = client.get("/health/live")
    assert "X-Request-ID" in r.headers
