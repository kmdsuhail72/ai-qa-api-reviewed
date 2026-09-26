import hmac
import time

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.metrics import RATE_LIMITED
from app.models import User, UserRole
from app.rate_limit import RateLimiter, get_global_rate_limiter, get_user_rate_limiter
from app.security import TokenError, decode_access_token

# HTTPBearer (not OAuth2PasswordBearer): /auth/login takes JSON, so the
# OAuth2 password-form flow in Swagger's "Authorize" dialog never worked.
# With HTTPBearer, Swagger lets you paste the token from /auth/login.
bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(detail: str = "Could not validate credentials") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _user_from_token(token: str, db: Session) -> User:
    try:
        payload = decode_access_token(token)
    except TokenError:
        raise _unauthorized()

    username = payload.get("sub")
    if not username:
        raise _unauthorized()

    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise _unauthorized("User no longer exists")
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled",
        )
    return user


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if creds is None or not creds.credentials:
        raise _unauthorized("Missing bearer token")
    return _user_from_token(creds.credentials, db)


def require_role(*allowed: UserRole):
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {[r.value for r in allowed]}",
            )
        return user

    return checker


# Role groups (single source of truth for the RBAC matrix)
CHAT_ROLES = (UserRole.admin, UserRole.user)
REPORT_ROLES = (UserRole.admin, UserRole.read_only)
ADMIN_ROLES = (UserRole.admin,)


def _raise_429(result, detail: str) -> None:
    retry_after = max(1, result.reset_at - int(time.time()))
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail.format(limit=result.limit, retry_after=retry_after),
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(result.limit),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(result.reset_at),
        },
    )


def enforce_rate_limit(
    user: User = Depends(require_role(*CHAT_ROLES)),
    user_limiter: RateLimiter = Depends(get_user_rate_limiter),
    global_limiter: RateLimiter = Depends(get_global_rate_limiter),
) -> User:
    """Chat authorization + per-user and global (cluster-wide) rate limiting.

    The global check runs first so a request rejected by the global cap does
    not also burn the user's personal quota.
    """
    global_result = global_limiter.check("global")
    if not global_result.allowed:
        RATE_LIMITED.labels(scope="global").inc()
        _raise_429(global_result, "Global rate limit exceeded. Try again in {retry_after}s.")

    user_result = user_limiter.check(f"user:{user.id}")
    if not user_result.allowed:
        RATE_LIMITED.labels(scope="user").inc()
        _raise_429(user_result, "Rate limit exceeded ({limit}/window). Try again in {retry_after}s.")

    return user


def require_metrics_access(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> None:
    """/metrics: static scrape token (for Prometheus) OR an admin JWT."""
    if creds is None or not creds.credentials:
        raise _unauthorized("Missing bearer token")
    token = creds.credentials
    scrape_token = get_settings().METRICS_TOKEN
    if scrape_token and hmac.compare_digest(token.encode(), scrape_token.encode()):
        return None
    user = _user_from_token(token, db)
    if user.role not in ADMIN_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return None
