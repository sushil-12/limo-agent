"""Read-only MySQL schema introspection for the debug routes.

Table names are validated against information_schema before use, and every query
uses bound parameters. Names are never interpolated into SQL.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.schemas.debug import (
    ColumnInfo,
    ForeignKeyInfo,
    IndexInfo,
    TableDetailResponse,
    TableSummary,
)

_TABLE_NAME = re.compile(r"^[A-Za-z0-9_$]{1,64}$")

_TABLES_SQL = text(
    """
    SELECT TABLE_NAME AS name, TABLE_ROWS AS estimated_rows, ENGINE AS engine
    FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
    ORDER BY TABLE_NAME
    """
)
_COLUMNS_SQL = text(
    """
    SELECT COLUMN_NAME AS name, COLUMN_TYPE AS type, IS_NULLABLE AS nullable,
           COLUMN_DEFAULT AS col_default, COLUMN_KEY AS col_key, EXTRA AS extra
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :name
    ORDER BY ORDINAL_POSITION
    """
)
_INDEXES_SQL = text(
    """
    SELECT INDEX_NAME AS name, NON_UNIQUE AS non_unique, COLUMN_NAME AS column_name
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :name
    ORDER BY INDEX_NAME, SEQ_IN_INDEX
    """
)
_FOREIGN_KEYS_SQL = text(
    """
    SELECT CONSTRAINT_NAME AS name, COLUMN_NAME AS column_name,
           REFERENCED_TABLE_NAME AS ref_table, REFERENCED_COLUMN_NAME AS ref_column
    FROM information_schema.KEY_COLUMN_USAGE
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :name
      AND REFERENCED_TABLE_NAME IS NOT NULL
    ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION
    """
)


class SchemaInspector:
    """Lists tables and describes one table of the connected database."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list_tables(self) -> list[TableSummary]:
        """All tables visible to this DB user, with InnoDB row estimates."""
        async with self._engine.connect() as conn:
            rows = (await conn.execute(_TABLES_SQL)).mappings().all()
        return [
            TableSummary(name=r["name"], estimated_rows=r["estimated_rows"], engine=r["engine"]) for r in rows
        ]

    async def describe_table(self, name: str) -> TableDetailResponse | None:
        """Describe ``name``, or return None if it is not a real table."""
        if not _TABLE_NAME.match(name):
            return None
        if name not in {t.name for t in await self.list_tables()}:
            return None
        return await self._describe(name)

    async def _describe(self, name: str) -> TableDetailResponse:
        """Query information_schema for an already-validated table name."""
        params = {"name": name}
        async with self._engine.connect() as conn:
            columns = (await conn.execute(_COLUMNS_SQL, params)).mappings().all()
            index_rows = (await conn.execute(_INDEXES_SQL, params)).mappings().all()
            fk_rows = (await conn.execute(_FOREIGN_KEYS_SQL, params)).mappings().all()

        indexes: dict[str, IndexInfo] = {}
        for r in index_rows:
            index = indexes.setdefault(
                r["name"], IndexInfo(name=r["name"], unique=not r["non_unique"], columns=[])
            )
            index.columns.append(r["column_name"])

        return TableDetailResponse(
            name=name,
            columns=[
                ColumnInfo(
                    name=c["name"],
                    type=c["type"],
                    nullable=c["nullable"] == "YES",
                    default=c["col_default"],
                    key=c["col_key"] or None,
                    extra=c["extra"] or None,
                )
                for c in columns
            ],
            indexes=list(indexes.values()),
            foreign_keys=[
                ForeignKeyInfo(
                    name=f["name"],
                    column=f["column_name"],
                    references_table=f["ref_table"],
                    references_column=f["ref_column"],
                )
                for f in fk_rows
            ],
        )
