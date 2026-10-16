"""Application factory: lifespan resources, middleware, error handlers and routers.

Run with ``uvicorn --factory app.main:create_app``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.routes import chat, debug_db, guest_chat, health, me
from app.auth.passport import PassportTokenVerifier
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import REQUEST_ID_HEADER, RequestContextMiddleware, configure_logging, get_logger
from app.core.middleware import BodySizeLimitMiddleware
from app.core.rate_limit import InMemoryRateLimiter
from app.db.session import create_engine, create_sessionmaker
from app.llm.base import LLMError, LLMProvider
from app.llm.factory import create_llm_provider, create_ollama_client
from app.services.chat_service import ChatService
from app.services.data.agent import DataAgent
from app.services.data.catalog import SchemaCatalog
from app.services.data.examples import load_examples
from app.services.data.policy import PolicyResolver
from app.services.data.query_log import QueryLog
from app.services.data.runner import QueryRunner
from app.services.health_service import HealthService
from app.services.schema_inspector import SchemaInspector
from app.services.topic_guard import TopicGuard

logger = get_logger(__name__)


async def _warm_up(provider: LLMProvider) -> None:
    """Load the model in the background so the first user doesn't pay load time."""
    started = time.perf_counter()
    try:
        await provider.warmup()
    except LLMError as exc:
        logger.warning("llm_warmup_failed", model=provider.model, error=str(exc))
        return
    logger.info(
        "llm_warmup_done",
        model=provider.model,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )


def _build_data_agent(
    settings: Settings,
    engine: AsyncEngine,
    http_client: httpx.AsyncClient,
    provider: LLMProvider,
    log_content: bool,
) -> DataAgent | None:
    """Text-to-SQL for signed-in users, or None when DATA_QUERIES_ENABLED=false."""
    if not settings.data_queries_enabled:
        return None
    sql_provider = (
        create_llm_provider(settings.llm_provider, settings, http_client, model=settings.ollama_sql_model)
        if settings.ollama_sql_model
        else provider
    )
    return DataAgent(
        sql_provider,
        SchemaCatalog(engine, ttl_seconds=settings.data_schema_cache_seconds),
        PolicyResolver(engine),
        QueryRunner(
            engine, timeout_seconds=settings.data_query_timeout_seconds, max_rows=settings.data_max_rows
        ),
        max_rows=settings.data_max_rows,
        max_attempts=settings.data_max_attempts,
        examples=load_examples(),
        query_log=QueryLog(settings.data_query_log_path) if settings.data_query_log_path else None,
        log_content=log_content,
    )


def _build_lifespan(
    settings: Settings, engine: AsyncEngine | None
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db_engine = engine or create_engine(settings)
        http_client = create_ollama_client(settings)
        provider = create_llm_provider(settings.llm_provider, settings, http_client)

        app.state.engine = db_engine
        app.state.sessionmaker = create_sessionmaker(db_engine)
        app.state.token_verifier = PassportTokenVerifier(
            db_engine,
            settings.passport_public_key_pem(),
            allowed_roles=settings.auth_allowed_roles,
            leeway_seconds=settings.passport_leeway_seconds,
            cache_ttl_seconds=settings.auth_cache_ttl_seconds,
            cache_max_entries=settings.auth_cache_max_entries,
        )
        app.state.chat_rate_limiter = InMemoryRateLimiter(settings.rate_limit_per_minute)
        app.state.guest_rate_limiters = (
            InMemoryRateLimiter(settings.guest_rate_limit_per_minute),
            InMemoryRateLimiter(settings.guest_rate_limit_per_hour, window_seconds=3600.0),
        )
        log_content = not settings.is_production and settings.log_level == "DEBUG"
        data_agent = _build_data_agent(settings, db_engine, http_client, provider, log_content)
        needs_guard = settings.topic_filter_enabled or data_agent is not None
        app.state.chat_service = ChatService(
            provider,
            num_ctx=settings.ollama_num_ctx,
            reserved_for_reply=settings.ollama_num_predict,
            topic_guard=(
                TopicGuard(provider, block_off_topic=settings.topic_filter_enabled) if needs_guard else None
            ),
            guest_max_messages=settings.guest_max_messages,
            data_agent=data_agent,
            log_content=log_content,
        )
        app.state.health_service = HealthService(
            db_engine,
            provider,
            timeout_seconds=settings.health_check_timeout_seconds,
            expose_details=not settings.is_production,
        )
        app.state.schema_inspector = SchemaInspector(db_engine)

        warmup = asyncio.create_task(_warm_up(provider)) if settings.ollama_warmup else None
        logger.info(
            "startup",
            env=settings.app_env,
            llm_provider=provider.name,
            model=provider.model,
            db_host=settings.db_host,
            db_database=settings.db_database,
            debug_routes=settings.enable_debug_routes,
            data_queries=data_agent is not None,
        )
        try:
            yield
        finally:
            if warmup is not None:
                warmup.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await warmup
            await http_client.aclose()
            await db_engine.dispose()
            logger.info("shutdown")

    return lifespan


def create_app(settings: Settings | None = None, *, engine: AsyncEngine | None = None) -> FastAPI:
    """Build the FastAPI app. ``engine`` lets tests inject a database."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_logs=settings.app_env != "local")

    app = FastAPI(
        title="Limo AI Agent API",
        version="0.1.0",
        lifespan=_build_lifespan(settings, engine),
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings

    # add_middleware wraps: the last added is outermost.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,  # bearer tokens, not cookies
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER, "Retry-After"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(me.router)
    app.include_router(chat.router)
    if settings.guest_chat_enabled:
        app.include_router(guest_chat.router)
    if settings.enable_debug_routes:
        app.include_router(debug_db.router)
    return app
