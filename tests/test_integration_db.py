"""Queries the real Laravel MySQL/MariaDB database configured in .env.

Skipped by default. Run with ``pytest -m integration tests/test_integration_db.py -v``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.db.session import create_engine

pytestmark = pytest.mark.integration

COUNT_QUERIES = {
    "users": "SELECT COUNT(*) FROM users",
    "tbl_account": "SELECT COUNT(*) FROM tbl_account",
    "oauth_clients": "SELECT COUNT(*) FROM oauth_clients",
    "oauth_access_tokens": "SELECT COUNT(*) FROM oauth_access_tokens",
}


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    settings = Settings(ollama_warmup=False)  # type: ignore[call-arg]  # reads .env
    engine = create_engine(settings)
    yield engine
    await engine.dispose()


async def test_connects(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        version = (await conn.execute(text("SELECT VERSION()"))).scalar_one()
        database = (await conn.execute(text("SELECT DATABASE()"))).scalar_one()
    print(f"\nconnected: server={version} database={database}")
    assert database


async def test_session_is_utc_and_read_only(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        tz = (await conn.execute(text("SELECT @@session.time_zone"))).scalar_one()
        assert tz == "+00:00"
        with pytest.raises(DBAPIError, match="READ ONLY"):
            await conn.execute(text("CREATE TEMPORARY TABLE zz_write_probe (x INT)"))


@pytest.mark.parametrize("table", COUNT_QUERIES)
async def test_laravel_table_readable(engine: AsyncEngine, table: str) -> None:
    async with engine.connect() as conn:
        rows = (await conn.execute(text(COUNT_QUERIES[table]))).scalar_one()
    print(f"\n{table}: {rows} rows")
    assert rows >= 0


async def test_users_by_role(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT role, COUNT(*) FROM users GROUP BY role ORDER BY role"))
        by_role = {role: count for role, count in result.all()}
    print(f"\nusers by role: {by_role}")
    assert by_role, "users table is empty"
