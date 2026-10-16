"""DB introspection for operators. Mounted only when ENABLE_DEBUG_ROUTES=true.

Requires the X-Service-Key header. Never used by frontends.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.api.deps import SchemaInspectorDep
from app.auth.dependencies import require_service_key
from app.core.errors import NotFoundError
from app.schemas.debug import TableDetailResponse, TableListResponse

router = APIRouter(
    prefix="/v1/debug/db",
    tags=["debug"],
    dependencies=[Depends(require_service_key)],
)


@router.get("/tables")
async def list_tables(inspector: SchemaInspectorDep) -> TableListResponse:
    """Tables with estimated row counts from information_schema."""
    return TableListResponse(tables=await inspector.list_tables())


@router.get("/tables/{name}")
async def describe_table(
    name: Annotated[str, Path(max_length=64)], inspector: SchemaInspectorDep
) -> TableDetailResponse:
    """Columns, types, nullability, defaults, indexes and foreign keys of one table."""
    detail = await inspector.describe_table(name)
    if detail is None:
        raise NotFoundError("Unknown table.")
    return detail
