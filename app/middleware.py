"""HTTP middleware: request_id, latency logging and Prometheus HTTP metrics."""
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.metrics import IN_FLIGHT, REQUEST_COUNT, REQUEST_LATENCY

logger = logging.getLogger("app.request")

# Probes/scrapes are excluded from logs and from the latency histogram so they
# don't drown out real traffic.
EXCLUDED_PATHS = {"/health/live", "/health/ready", "/metrics"}


def _route_template(request: Request) -> str:
    """Use the route template (/chat, /admin/users/{user_id}) - never the raw
    path - so metric label cardinality stays bounded."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or "unmatched"


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        path = request.url.path
        track = path not in EXCLUDED_PATHS

        if track:
            IN_FLIGHT.inc()
        start = time.perf_counter()
        status_code = 500
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
        except Exception:
            logger.exception(
                "request_failed",
                extra={"request_id": request_id, "endpoint": path,
                       "latency_ms": round((time.perf_counter() - start) * 1000, 2)},
            )
            raise
        finally:
            elapsed = time.perf_counter() - start
            if track:
                IN_FLIGHT.dec()
                endpoint = _route_template(request)
                REQUEST_COUNT.labels(method=request.method, endpoint=endpoint,
                                     status=str(status_code)).inc()
                REQUEST_LATENCY.labels(method=request.method, endpoint=endpoint).observe(elapsed)

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-ms"] = f"{elapsed * 1000:.2f}"
        if track:
            logger.info(
                "request_completed",
                extra={"request_id": request_id, "method": request.method,
                       "endpoint": path, "status": status_code,
                       "latency_ms": round(elapsed * 1000, 2)},
            )
        return response
