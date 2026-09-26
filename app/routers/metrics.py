from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from app.config import get_settings
from app.deps import require_metrics_access
from app.metrics import render_metrics

router = APIRouter(tags=["metrics"])


@router.get("/metrics", dependencies=[Depends(require_metrics_access)])
def metrics() -> Response:
    """Prometheus exposition. Auth: admin JWT, or the METRICS_TOKEN bearer
    token for Prometheus scrapers (see k8s/base/servicemonitor.example.yaml)."""
    if not get_settings().METRICS_ENABLED:
        raise HTTPException(status_code=404, detail="Metrics disabled")
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)
