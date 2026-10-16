"""Async SQLAlchemy engine and sessions for the existing Laravel MySQL database.

Laravel owns the schema. This module never creates, alters or migrates tables.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings

DRIVER = "mysql+aiomysql"


def build_database_url(settings: Settings) -> URL:
    """Build the URL with URL.create so special characters in passwords are safe."""
    return URL.create(
        DRIVER,
        username=settings.db_username,
        password=settings.db_password.get_secret_value(),
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_database,
        query={"charset": "utf8mb4"},
    )


def session_init_sql(settings: Settings) -> str:
    """SQL run on every new connection: fixed time zone, optional read-only session.

    The time zone must match Laravel's config/app.php 'timezone' (UTC) so TIMESTAMP
    columns come back in the same zone Laravel wrote them in.

    SET SESSION TRANSACTION READ ONLY works on MySQL 5.7/8 and MariaDB; the variable
    names don't (MariaDB < 11.1 lacks transaction_read_only, MySQL 8 dropped tx_read_only).
    """
    sql = f"SET time_zone = '{_quote_tz(settings.db_timezone)}'"
    if settings.db_read_only:
        sql += "; SET SESSION TRANSACTION READ ONLY"
    return sql


def _quote_tz(tz: str) -> str:
    if not all(ch.isalnum() or ch in "+-:/_" for ch in tz):
        raise ValueError("DB_TIMEZONE contains invalid characters")
    return tz


def create_engine(settings: Settings) -> AsyncEngine:
    """Create the pooled async engine."""
    return create_async_engine(
        build_database_url(settings),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle,
        pool_timeout=settings.db_pool_timeout,
        pool_pre_ping=True,
        echo=settings.db_echo,
        connect_args={
            "connect_timeout": settings.db_connect_timeout,
            "init_command": session_init_sql(settings),
        },
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory; expire_on_commit=False suits async request handlers."""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session that is closed after the request."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with factory() as session:
        yield session
