"""Executes guarded SELECTs with a server-side time limit and JSON-safe results."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

_MAX_CELL_CHARS = 300
# Statement time limits differ: MySQL 8 has max_execution_time (ms, ER_QUERY_TIMEOUT 3024),
# MariaDB (e.g. local XAMPP) has max_statement_time (seconds, ER_STATEMENT_TIMEOUT 1969).
_TIMEOUT_ERRORS = frozenset({3024, 1969})
_LIMIT_SQL = {
    False: ("SET SESSION max_execution_time = :value", "SET SESSION max_execution_time = 0"),
    True: ("SET SESSION max_statement_time = :value", "SET SESSION max_statement_time = 0"),
}


@dataclass(frozen=True, slots=True)
class QueryRows:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool  # the row cap was reached; more rows may exist
    elapsed_ms: float


class QueryFailedError(Exception):
    """The database rejected the query. The message is for the model, not clients."""


class QueryTimeoutError(Exception):
    """The query ran longer than the limit."""


class QueryRunner:
    def __init__(self, engine: AsyncEngine, *, timeout_seconds: float, max_rows: int) -> None:
        self._engine = engine
        self._timeout = timeout_seconds
        self._max_rows = max_rows

    @property
    def dialect(self) -> str:
        return self._engine.dialect.name

    async def run(self, sql: str) -> QueryRows:
        """Run one already-guarded SELECT. ``no_parameters`` stops ``%`` / ``:x`` being read as params."""
        started = time.perf_counter()
        mysql = self._engine.dialect.name == "mysql"
        try:
            # The client-side timeout is a backstop; the server stops the statement itself.
            async with asyncio.timeout(self._timeout + 2), self._engine.connect() as conn:
                mariadb = bool(getattr(conn.dialect, "is_mariadb", False))
                set_limit, reset_limit = _LIMIT_SQL[mariadb]
                if mysql:
                    value = self._timeout if mariadb else int(self._timeout * 1000)
                    await conn.execute(text(set_limit), {"value": value})
                try:
                    result = await conn.exec_driver_sql(sql, execution_options={"no_parameters": True})
                    columns = list(result.keys())
                    raw = result.fetchmany(self._max_rows + 1)
                finally:
                    if mysql:
                        await _reset_limit(conn, reset_limit)
        except TimeoutError as exc:
            raise QueryTimeoutError from exc
        except DBAPIError as exc:
            orig = exc.orig
            if orig is not None and getattr(orig, "args", (None,))[0] in _TIMEOUT_ERRORS:
                raise QueryTimeoutError from exc
            raise QueryFailedError(str(orig or exc).splitlines()[0][:300]) from exc
        return QueryRows(
            columns=columns,
            rows=[[_json_safe(v) for v in row] for row in raw[: self._max_rows]],
            truncated=len(raw) > self._max_rows,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )


async def _reset_limit(conn: AsyncConnection, sql: str) -> None:
    """Pooled connections are shared with auth lookups, so the limit must not linger."""
    try:
        await conn.execute(text(sql))
    except (DBAPIError, OSError):
        await conn.invalidate()  # broken mid-query (e.g. timed out): drop it from the pool


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):  # MySQL TIME columns arrive as timedelta
        seconds = int(value.total_seconds())
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    if isinstance(value, bytes | bytearray):
        return "<binary>"
    value = str(value)
    return value if len(value) <= _MAX_CELL_CHARS else value[:_MAX_CELL_CHARS] + "..."
