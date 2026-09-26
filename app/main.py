import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.config import get_settings
from app.database import Base, check_db, engine
from app.logging_config import configure_logging
from app.middleware import RequestContextMiddleware
from app.redis_client import check_redis
from app.routers import admin, auth, chat, metrics, reports
from app.schemas import ComponentStatus, HealthResponse, HealthStatus

settings = get_settings()
configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.DB_AUTO_CREATE:
        _create_schema()
    _seed_users()
    from app.metrics import APP_INFO

    APP_INFO.info({"version": settings.SERVICE_VERSION, "env": settings.ENV})
    logger.info("startup_complete", extra={"env": settings.ENV, "version": settings.SERVICE_VERSION})
    yield
    await chat.shutdown_gateway()


def _create_schema() -> None:
    """Idempotent table creation. Previously this only ran when
    ENV in {development, test}, so ENV=production (scale compose, k8s) booted
    against an empty database and every login failed with 'relation users
    does not exist'. Replicas racing on first boot are tolerated."""
    try:
        Base.metadata.create_all(bind=engine)
    except SQLAlchemyError as e:
        logger.warning("schema_create_failed", extra={"error": str(e)[:300]})


def _seed_users() -> None:
    """Create bootstrap users ONLY from env-provided passwords.

    No credentials are hard-coded in source. If a password env var is empty,
    that user is simply not created.
    """
    from app.database import SessionLocal
    from app.models import User, UserRole
    from app.security import hash_password

    candidates = [
        (settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD, UserRole.admin, settings.ADMIN_EMAIL),
        ("user", settings.DEMO_USER_PASSWORD, UserRole.user, None),
        ("readonly", settings.DEMO_READONLY_PASSWORD, UserRole.read_only, None),
    ]
    db = SessionLocal()
    try:
        for username, password, role, email in candidates:
            if not password:
                continue
            if db.query(User).filter(User.username == username).first():
                continue
            db.add(User(username=username, email=email, hashed_password=hash_password(password),
                        role=role, is_active=True))
            try:
                db.commit()
            except IntegrityError:  # another replica seeded it first
                db.rollback()
    except SQLAlchemyError as e:
        db.rollback()
        logger.warning("seed_users_failed", extra={"error": str(e)[:300]})
    finally:
        db.close()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.SERVICE_VERSION,
    description="AI Question-Answering API: JWT auth, LLM gateway with retries/fallback, "
    "Redis cache + distributed rate limiting, PostgreSQL persistence, Prometheus metrics.",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(RequestContextMiddleware)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    logger.exception("unhandled_exception", extra={"request_id": request_id, "endpoint": request.url.path})
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error", "request_id": request_id},
    )


frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if frontend_dir.is_dir():
    app.mount("/ui", StaticFiles(directory=frontend_dir, html=True), name="frontend")


@app.get("/", tags=["root"])
def root() -> dict:
    return {"status": "ok", "app": settings.APP_NAME, "env": settings.ENV}


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["health"],
    responses={503: {"model": HealthResponse, "description": "Database unavailable"}},
)
def health() -> JSONResponse:
    """Deep health check (required endpoint).

    * ``ok``       - database and Redis reachable.
    * ``degraded`` - Redis down: the API still serves requests (cache misses,
      rate limiter fails open), so this returns 200.
    * ``down``     - database down: login/auth cannot work -> 503.
    """
    db_ok = check_db()
    redis_ok = check_redis()
    if not db_ok:
        overall, code = HealthStatus.down, status.HTTP_503_SERVICE_UNAVAILABLE
    elif not redis_ok:
        overall, code = HealthStatus.degraded, status.HTTP_200_OK
    else:
        overall, code = HealthStatus.ok, status.HTTP_200_OK
    body = HealthResponse(
        status=overall,
        database=ComponentStatus.ok if db_ok else ComponentStatus.error,
        redis=ComponentStatus.ok if redis_ok else ComponentStatus.error,
        version=settings.SERVICE_VERSION,
    )
    return JSONResponse(status_code=code, content=body.model_dump(mode="json"))


@app.get("/health/live", tags=["health"])
def liveness() -> dict:
    """Liveness: process is up. Never checks dependencies (a Redis blip
    must not make Kubernetes restart every pod)."""
    return {"status": "alive"}


@app.get("/health/ready", response_model=None, tags=["health"])
def readiness() -> dict | JSONResponse:
    """Readiness: can this pod serve traffic? Requires the database (auth
    depends on it). Redis is reported but is NOT fatal, because the app
    degrades gracefully without it; failing readiness on a shared Redis
    outage would pull every pod out of the load balancer at once."""
    checks = {"database": check_db(), "redis": check_redis()}
    if not checks["database"]:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "checks": checks},
        )
    return {"status": "ready" if checks["redis"] else "degraded", "checks": checks}


app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(admin.router)
app.include_router(reports.router)
app.include_router(metrics.router)
