import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.cache import Cache, get_cache
from app.database import get_db
from app.deps import enforce_rate_limit, get_current_user
from app.llm import (
    LLMError,
    LLMGateway,
    LLMOverloaded,
    LLMRateLimited,
    LLMTimeout,
    build_gateway,
)
from app.metrics import CACHE_HITS, CACHE_MISSES, CACHE_WRITES, CHAT_LOGS_WRITTEN, CHAT_REQUESTS
from app.models import ChatLog, User
from app.schemas import ChatHistoryItem, ChatRequest, ChatResponse, ErrorResponse

logger = logging.getLogger("app.chat")
router = APIRouter(prefix="/chat", tags=["chat"])

# One gateway per process: it owns the pooled HTTP client and the
# concurrency semaphore, so it must NOT be rebuilt per request (the previous
# version constructed a new provider + gateway on every call).
_gateway: LLMGateway | None = None


def get_gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        _gateway = build_gateway()
    return _gateway


async def shutdown_gateway() -> None:
    global _gateway
    if _gateway is not None:
        await _gateway.aclose()
        _gateway = None


def _persist(db: Session, log: ChatLog) -> None:
    """Best-effort audit write. A database hiccup must not turn an already
    generated (and paid for) answer into a 500 for the user."""
    try:
        db.add(log)
        db.commit()
        CHAT_LOGS_WRITTEN.labels(status=log.status).inc()
    except SQLAlchemyError as e:
        db.rollback()
        CHAT_LOGS_WRITTEN.labels(status="db_error").inc()
        logger.error("chat_log_write_failed", extra={"error": str(e)[:300]})


def _llm_http_error(exc: LLMError) -> HTTPException:
    if isinstance(exc, LLMTimeout):
        outcome, code, detail, retry = "llm_timeout", status.HTTP_504_GATEWAY_TIMEOUT, \
            "The LLM provider timed out. Please try again.", 5
    elif isinstance(exc, LLMRateLimited):
        outcome, code, detail = "llm_rate_limited", status.HTTP_503_SERVICE_UNAVAILABLE, \
            "The LLM provider is rate limiting requests. Please retry shortly."
        retry = int(exc.retry_after or 10)
    elif isinstance(exc, LLMOverloaded):
        outcome, code, detail, retry = "llm_overloaded", status.HTTP_503_SERVICE_UNAVAILABLE, \
            "The service is at capacity. Please retry shortly.", 2
    else:
        outcome, code, detail, retry = "llm_error", status.HTTP_502_BAD_GATEWAY, \
            "LLM provider is currently unavailable. Please try again.", 5
    CHAT_REQUESTS.labels(outcome=outcome).inc()
    return HTTPException(status_code=code, detail=detail, headers={"Retry-After": str(max(1, retry))})


_ERRORS = {
    401: {"model": ErrorResponse, "description": "Missing/invalid token"},
    403: {"description": "Role not allowed to chat (read_only)"},
    429: {"description": "Per-user or global rate limit exceeded"},
    502: {"description": "LLM provider error after retries + fallback"},
    503: {"description": "LLM provider rate limited or local concurrency limit reached"},
    504: {"description": "LLM timed out (per-call or total budget)"},
}


@router.post("", response_model=ChatResponse, responses=_ERRORS)
async def chat(
    payload: ChatRequest,
    user: User = Depends(enforce_rate_limit),
    db: Session = Depends(get_db),
    gateway: LLMGateway = Depends(get_gateway),
    cache: Cache = Depends(get_cache),
) -> ChatResponse:
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="question must not be blank")

    started = time.perf_counter()

    # 1. Cache lookup (sync Redis client -> threadpool so the event loop
    #    never blocks on network I/O).
    cached = await run_in_threadpool(cache.get_model, question, ChatResponse)
    if cached is not None:
        CACHE_HITS.inc()
        CHAT_REQUESTS.labels(outcome="cache_hit").inc()
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        await run_in_threadpool(_persist, db, ChatLog(
            user_id=user.id, question=question, answer=cached.answer, model=cached.model,
            prompt_tokens=0, completion_tokens=0, latency_ms=latency_ms, status="cache_hit",
        ))
        # Tokens reported are what the ORIGINAL generation cost; this request
        # consumed 0 new tokens (recorded as such in the DB + metrics).
        return cached.model_copy(update={"cached": True, "latency_ms": latency_ms})
    CACHE_MISSES.inc()

    # 2. LLM call via gateway (timeouts, retries, fallback, concurrency cap)
    try:
        result = await gateway.answer(question)
    except LLMError as e:
        latency_ms = (time.perf_counter() - started) * 1000
        await run_in_threadpool(_persist, db, ChatLog(
            user_id=user.id, question=question, answer="", model="",
            status="error", latency_ms=latency_ms,
        ))
        logger.warning("chat_llm_failed", extra={"user_id": user.id, "error": type(e).__name__})
        raise _llm_http_error(e) from e

    latency_ms = (time.perf_counter() - started) * 1000

    # 3. Persist (best effort)
    await run_in_threadpool(_persist, db, ChatLog(
        user_id=user.id, question=question, answer=result.answer,
        model=result.model, prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens, latency_ms=latency_ms,
        status="fallback" if result.fallback_used else "success",
    ))

    response = ChatResponse(
        answer=result.answer,
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        latency_ms=round(latency_ms, 2),
        fallback_used=result.fallback_used,
        cached=False,
    )

    # 4. Cache write
    await run_in_threadpool(cache.set_model, question, response)
    CACHE_WRITES.inc()
    CHAT_REQUESTS.labels(outcome="success").inc()

    logger.info("chat_success", extra={
        "user_id": user.id, "model": result.model, "latency_ms": round(latency_ms, 2),
        "prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens,
        "fallback": result.fallback_used,
    })
    return response


@router.get("/history", response_model=list[ChatHistoryItem])
def history(
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ChatHistoryItem]:
    """The caller's own recent Q&A (any authenticated role)."""
    rows = (
        db.query(ChatLog)
        .filter(ChatLog.user_id == user.id)
        .order_by(ChatLog.created_at.desc(), ChatLog.id.desc())
        .limit(limit)
        .all()
    )
    return [ChatHistoryItem.model_validate(r) for r in rows]
