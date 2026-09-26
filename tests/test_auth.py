"""Tests for authentication and RBAC."""

from tests.conftest import auth_header


def test_login_success(client):
    r = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200
    body = r.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert body["role"] == "admin"
    assert body["expires_in"] > 0


def test_login_wrong_password(client):
    r = client.post("/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    assert "Invalid" in r.json()["detail"]


def test_login_unknown_user(client):
    r = client.post("/auth/login", json={"username": "ghost", "password": "x"})
    assert r.status_code == 401


def test_login_validation_error(client):
    r = client.post("/auth/login", json={"username": "a"})  # missing password
    assert r.status_code == 422


def test_me_requires_token(client):
    r = client.get("/auth/me")
    assert r.status_code == 401


def test_me_with_valid_token(client, admin_token):
    r = client.get("/auth/me", headers=auth_header(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "admin"
    assert body["role"] == "admin"
    assert body["is_active"] is True


def test_me_with_invalid_token(client):
    r = client.get("/auth/me", headers=auth_header("not-a-real-jwt"))
    assert r.status_code == 401


def test_rbac_admin_only_endpoint_requires_admin(client, user_token):
    """Regular user cannot access /metrics."""
    r = client.get("/metrics", headers=auth_header(user_token))
    assert r.status_code == 403


def test_rbac_admin_can_access_metrics(client, admin_token):
    r = client.get("/metrics", headers=auth_header(admin_token))
    assert r.status_code == 200
