"""Aggregate usage reporting (RBAC: admin + read_only).

This is the "permitted reports/data" the read-only role can access: totals
only, never other users' questions or answers.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import REPORT_ROLES, require_role
from app.models import ChatLog, User
from app.schemas import ModelUsage, UsageReport

router = APIRouter(prefix="/reports", tags=["reports"])

_SUCCESS = ("success", "fallback", "cache_hit")


@router.get("/usage", response_model=UsageReport)
def usage(
    hours: int = Query(24, ge=1, le=24 * 90),
    _: User = Depends(require_role(*REPORT_ROLES)),
    db: Session = Depends(get_db),
) -> UsageReport:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    q = db.query(ChatLog).filter(ChatLog.created_at >= since)

    totals = q.with_entities(
        func.count(ChatLog.id),
        func.coalesce(func.sum(case((ChatLog.status.in_(_SUCCESS), 1), else_=0)), 0),
        func.coalesce(func.sum(ChatLog.prompt_tokens), 0),
        func.coalesce(func.sum(ChatLog.completion_tokens), 0),
        func.coalesce(func.avg(ChatLog.latency_ms), 0.0),
    ).one()
    total, ok, p_tok, c_tok, avg_lat = totals

    rows = (
        q.filter(ChatLog.model != "")
        .with_entities(
            ChatLog.model,
            func.count(ChatLog.id),
            func.coalesce(func.sum(ChatLog.prompt_tokens), 0),
            func.coalesce(func.sum(ChatLog.completion_tokens), 0),
            func.coalesce(func.avg(ChatLog.latency_ms), 0.0),
        )
        .group_by(ChatLog.model)
        .order_by(func.count(ChatLog.id).desc())
        .all()
    )
    return UsageReport(
        window_hours=hours,
        total_requests=int(total),
        successful_requests=int(ok),
        failed_requests=int(total) - int(ok),
        prompt_tokens=int(p_tok),
        completion_tokens=int(c_tok),
        total_tokens=int(p_tok) + int(c_tok),
        avg_latency_ms=round(float(avg_lat), 2),
        by_model=[
            ModelUsage(model=m, requests=int(n), prompt_tokens=int(p), completion_tokens=int(c),
                       avg_latency_ms=round(float(lat), 2))
            for m, n, p, c, lat in rows
        ],
    )
