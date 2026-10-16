"""Dependencies that fetch app-scoped services and enforce rate limits."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.auth.dependencies import CurrentUserDep
from app.auth.models import CurrentUser
from app.core.errors import RateLimitedError
from app.core.rate_limit import RateLimiter
from app.services.chat_service import ChatService
from app.services.health_service import HealthService
from app.services.requester import guest_key
from app.services.schema_inspector import SchemaInspector


def get_chat_service(request: Request) -> ChatService:
    return request.app.state.chat_service  # type: ignore[no-any-return]


def get_health_service(request: Request) -> HealthService:
    return request.app.state.health_service  # type: ignore[no-any-return]


def get_schema_inspector(request: Request) -> SchemaInspector:
    return request.app.state.schema_inspector  # type: ignore[no-any-return]


async def chat_rate_limited_user(request: Request, user: CurrentUserDep) -> CurrentUser:
    """Authenticate, then apply the per-user chat rate limit (429 when exceeded)."""
    limiter: RateLimiter = request.app.state.chat_rate_limiter
    result = await limiter.hit(f"chat:{user.id}")
    if not result.allowed:
        raise RateLimitedError(result.retry_after_seconds)
    return user


async def guest_rate_limited_key(request: Request) -> str:
    """Key a guest by client IP and apply the per-minute and per-hour guest limits.

    Behind a reverse proxy, the real client IP comes from X-Forwarded-For only when
    the proxy is listed in FORWARDED_ALLOW_IPS (uvicorn --proxy-headers).
    """
    key = guest_key(request.client.host if request.client else None)
    limiters: tuple[RateLimiter, ...] = request.app.state.guest_rate_limiters
    for limiter in limiters:
        result = await limiter.hit(f"guest:{key}")
        if not result.allowed:
            raise RateLimitedError(result.retry_after_seconds)
    return key


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]
HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]
SchemaInspectorDep = Annotated[SchemaInspector, Depends(get_schema_inspector)]
ChatUserDep = Annotated[CurrentUser, Depends(chat_rate_limited_user)]
GuestKeyDep = Annotated[str, Depends(guest_rate_limited_key)]
