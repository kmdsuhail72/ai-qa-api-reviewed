"""Pytest fixtures for the AI QA API test suite.

These fixtures make the app testable without requiring real Postgres,
Redis, or Ollama. Tests use SQLite in-memory and fakeredis.
"""
import os

# Must be set BEFORE importing the app
os.environ.setdefault("ENV", "test")
os.environ.setdefault("JWT_SECRET", "test-secret-do-not-use-in-prod")
os.environ.setdefault("JWT_ISSUER", "ai-qa-api")
os.environ.setdefault("JWT_AUDIENCE", "ai-qa-api-clients")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://test.invalid/v1")
os.environ.setdefault("LLM_MODEL", "test-model")
os.environ.setdefault("LLM_FALLBACK_MODEL", "test-fallback")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("DEMO_USER_PASSWORD", "user123")
os.environ.setdefault("DEMO_READONLY_PASSWORD", "read123")
os.environ.setdefault("METRICS_TOKEN", "test-scrape-token")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("BCRYPT_ROUNDS", "4")  # fast hashing in tests only
os.environ.setdefault("CACHE_ENABLED", "true")
os.environ.setdefault("RATE_LIMIT_ENABLED", "true")
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "60")

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Now safe to import app modules
from app.cache import Cache, get_cache  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.llm.base import LLMResult  # noqa: E402
from app.main import app  # noqa: E402
from app.rate_limit import (  # noqa: E402
    RateLimiter,
    get_global_rate_limiter,
    get_user_rate_limiter,
)
from app.routers.chat import get_gateway  # noqa: E402


# ---------- Database ----------
@pytest.fixture(scope="function")
def db_engine():
    """Fresh in-memory SQLite per test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def TestSessionLocal(db_engine):
    return sessionmaker(bind=db_engine, autoflush=False, autocommit=False)


@pytest.fixture(scope="function")
def _override_db(TestSessionLocal):
    def _get_test_db():
        s = TestSessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_test_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(scope="function", autouse=True)
def _seed_users(TestSessionLocal, _override_db):
    """Seed the three default users for every test."""
    from app.models import User, UserRole
    from app.security import hash_password

    db = TestSessionLocal()
    db.add_all([
        User(username="admin", hashed_password=hash_password("admin123"),
             role=UserRole.admin, is_active=True),
        User(username="user", hashed_password=hash_password("user123"),
             role=UserRole.user, is_active=True),
        User(username="readonly", hashed_password=hash_password("read123"),
             role=UserRole.read_only, is_active=True),
    ])
    db.commit()
    db.close()


# ---------- Fake Redis / Cache ----------
@pytest.fixture(autouse=True)
def fake_redis():
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()


@pytest.fixture(autouse=True)
def fake_cache(fake_redis):
    cache = Cache(fake_redis, prefix="test:chat:", ttl_seconds=60, enabled=True)
    app.dependency_overrides[get_cache] = lambda: cache
    yield cache
    app.dependency_overrides.pop(get_cache, None)


# ---------- Fake rate limiters ----------
@pytest.fixture(autouse=True)
def fake_rate_limiters(fake_redis):
    """Generous limit so tests aren't rate-limited unless they test it explicitly."""
    user_limiter = RateLimiter(fake_redis, limit=60, window_seconds=60, enabled=True)
    global_limiter = RateLimiter(fake_redis, limit=10_000, window_seconds=60, enabled=True)
    app.dependency_overrides[get_user_rate_limiter] = lambda: user_limiter
    app.dependency_overrides[get_global_rate_limiter] = lambda: global_limiter
    yield user_limiter, global_limiter
    app.dependency_overrides.pop(get_user_rate_limiter, None)
    app.dependency_overrides.pop(get_global_rate_limiter, None)


# ---------- Fake LLM gateway ----------
class FakeGateway:
    """Deterministic stand-in for LLMGateway."""

    def __init__(self, *, result=None, raises=None):
        self._result = result or LLMResult(
            answer="Test answer", model="test-model",
            prompt_tokens=10, completion_tokens=5,
        )
        self._raises = raises
        self.primary_model = "test-model"
        self.calls = []

    async def answer(self, question: str):
        self.calls.append(question)
        if self._raises is not None:
            raise self._raises
        return self._result


@pytest.fixture
def fake_gateway_factory():
    def _install(fake):
        app.dependency_overrides[get_gateway] = lambda: fake
        return fake
    yield _install
    app.dependency_overrides.pop(get_gateway, None)


# ---------- TestClient ----------
@pytest.fixture(scope="function")
def client(_override_db):
    with TestClient(app) as c:
        yield c


# ---------- Auth helpers ----------
def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_token(client) -> str:
    r = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture
def user_token(client) -> str:
    r = client.post("/auth/login", json={"username": "user", "password": "user123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture
def readonly_token(client) -> str:
    r = client.post("/auth/login", json={"username": "readonly", "password": "read123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]
