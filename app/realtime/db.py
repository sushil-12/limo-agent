"""The realtime service's own database engine (writer account RT_DB_USERNAME).

Separate from ``app.db.session`` on purpose: the chatbot's engine is read-only and must
stay that way. Which tables this account can touch is enforced by MySQL grants
(docs/realtime/GRANTS.sql), not by this code.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.realtime.settings import RealtimeSettings

DRIVER = "mysql+aiomysql"


def build_rt_database_url(settings: RealtimeSettings) -> URL:
    return URL.create(
        DRIVER,
        username=settings.rt_db_username,
        password=settings.rt_db_password.get_secret_value(),
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_database,
        query={"charset": "utf8mb4"},
    )


def create_rt_engine(settings: RealtimeSettings) -> AsyncEngine:
    if not all(ch.isalnum() or ch in "+-:/_" for ch in settings.db_timezone):
        raise ValueError("DB_TIMEZONE contains invalid characters")
    return create_async_engine(
        build_rt_database_url(settings),
        pool_size=settings.rt_db_pool_size,
        max_overflow=settings.rt_db_max_overflow,
        pool_recycle=settings.rt_db_pool_recycle,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": settings.rt_db_connect_timeout,
            # Same zone as Laravel (UTC) so TIMESTAMP columns round-trip unchanged.
            "init_command": f"SET time_zone = '{settings.db_timezone}'",
        },
    )


def utcnow() -> datetime:
    """Naive UTC, the way Laravel stores timestamps."""
    return datetime.now(UTC).replace(tzinfo=None)
