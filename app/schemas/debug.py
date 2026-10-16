"""Schemas for the service-key-only DB introspection routes."""

from __future__ import annotations

from pydantic import BaseModel


class TableSummary(BaseModel):
    name: str
    estimated_rows: int | None
    engine: str | None


class TableListResponse(BaseModel):
    tables: list[TableSummary]


class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool
    default: str | None
    key: str | None
    extra: str | None


class IndexInfo(BaseModel):
    name: str
    unique: bool
    columns: list[str]


class ForeignKeyInfo(BaseModel):
    name: str
    column: str
    references_table: str
    references_column: str


class TableDetailResponse(BaseModel):
    name: str
    columns: list[ColumnInfo]
    indexes: list[IndexInfo]
    foreign_keys: list[ForeignKeyInfo]
