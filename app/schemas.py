from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import UserRole


class HealthStatus(str, Enum):
    ok = "ok"
    degraded = "degraded"
    down = "down"


class ComponentStatus(str, Enum):
    ok = "ok"
    error = "error"


class HealthResponse(BaseModel):
    status: HealthStatus
    database: ComponentStatus
    redis: ComponentStatus
    version: str


class ErrorResponse(BaseModel):
    error: str
    detail: str
    request_id: Optional[str] = None


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: UserRole


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: Optional[EmailStr] = None
    role: UserRole
    is_active: bool
    created_at: datetime


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    answer: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int = 0
    latency_ms: float
    fallback_used: bool = False
    cached: bool = False


class ChatHistoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    question: str
    answer: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    status: str
    created_at: datetime


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(..., min_length=8, max_length=128)
    email: Optional[EmailStr] = None
    role: UserRole = UserRole.user


class UserUpdate(BaseModel):
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None


class ModelUsage(BaseModel):
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    avg_latency_ms: float


class UsageReport(BaseModel):
    window_hours: int
    total_requests: int
    successful_requests: int
    failed_requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    avg_latency_ms: float
    by_model: list[ModelUsage]
