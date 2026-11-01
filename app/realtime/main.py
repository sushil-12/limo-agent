"""limo-rt application factory.

Run with::

    uvicorn --factory app.realtime.main:create_app --port 9100 \
        --ws-ping-interval 20 --ws-ping-timeout 20 --no-access-log

One process: WebSocket, REST, internal API and background jobs. State lives in Redis,
so a restart costs clients a reconnect, not data.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import register_exception_handlers
from app.core.logging import REQUEST_ID_HEADER, RequestContextMiddleware, configure_logging, get_logger
from app.core.middleware import BodySizeLimitMiddleware
from app.realtime import api, internal, ocr_api, ws
from app.realtime.db import create_rt_engine
from app.realtime.jobs import Jobs
from app.realtime.services import build_realtime
from app.realtime.settings import RealtimeSettings, get_realtime_settings

logger = get_logger(__name__)

MAX_BODY_BYTES = 64 * 1024


class PathBodyLimit:
    """64 KB for every route except licence uploads (image + base64 overhead)."""

    def __init__(self, app: ASGIApp, *, default: int, overrides: dict[str, int]) -> None:
        self._default = BodySizeLimitMiddleware(app, default)
        self._overrides = {path: BodySizeLimitMiddleware(app, size) for path, size in overrides.items()}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        handler = self._overrides.get(scope.get("path", "")) if scope["type"] == "http" else None
        await (handler or self._default)(scope, receive, send)


def _build_lifespan(
    settings: RealtimeSettings, engine: AsyncEngine | None, redis: Redis | None
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db_engine = engine or create_rt_engine(settings)
        redis_client = redis or Redis.from_url(
            settings.redis_url, socket_timeout=5, socket_connect_timeout=3, health_check_interval=30
        )
        http = httpx.AsyncClient(timeout=10.0, limits=httpx.Limits(max_connections=50))
        rt = build_realtime(settings, db_engine, redis_client, http)
        app.state.rt = rt
        jobs = Jobs(rt)
        await rt.flags.load()
        rt.flags.start()
        await rt.hub.start()
        jobs.start()
        logger.info(
            "rt_startup",
            env=settings.app_env,
            db_host=settings.db_host,
            db_user=settings.rt_db_username,
            fcm=rt.fcm.enabled,
            provider_flag=rt.flags.get("rt.provider"),
        )
        try:
            yield
        finally:
            await jobs.stop()
            await rt.hub.stop()
            await rt.tasks.drain()
            await rt.flags.stop()
            await http.aclose()
            if redis is None:
                await redis_client.aclose()
            if engine is None:
                await db_engine.dispose()
            logger.info("rt_shutdown")

    return lifespan


def create_app(
    settings: RealtimeSettings | None = None,
    *,
    engine: AsyncEngine | None = None,
    redis: Redis | None = None,
) -> FastAPI:
    """Build limo-rt. ``engine`` and ``redis`` let tests inject fakes."""
    settings = settings or get_realtime_settings()
    configure_logging(settings.log_level, json_logs=settings.app_env != "local")
    app = FastAPI(
        title="Limo Realtime (limo-rt)",
        version="1.0.0",
        lifespan=_build_lifespan(settings, engine, redis),
        docs_url="/rt/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/rt/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    upload_limit = settings.documentai_max_file_size_bytes * 4 // 3 + 64 * 1024  # base64 JSON + headers
    app.add_middleware(PathBodyLimit, default=MAX_BODY_BYTES, overrides={ocr_api.SCAN_PATH: upload_limit})
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.rt_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER, "Retry-After"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(ws.router)
    app.include_router(api.router)
    app.include_router(ocr_api.router)
    app.include_router(internal.router)
    app.include_router(internal.public_router)
    return app
