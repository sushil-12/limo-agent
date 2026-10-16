"""Service-key-only DB introspection routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.schemas.debug import ColumnInfo, TableDetailResponse, TableSummary
from app.services.schema_inspector import SchemaInspector
from tests.conftest import RIDER_ID, SERVICE_KEY, TokenFactory, auth

KEY_HEADER = {"X-Service-Key": SERVICE_KEY}


class FakeInspector(SchemaInspector):
    """SQLite has no information_schema: fake the queries, keep the real validation."""

    def __init__(self) -> None:
        self.described: list[str] = []

    async def list_tables(self) -> list[TableSummary]:
        return [TableSummary(name="users", estimated_rows=5, engine="InnoDB")]

    async def _describe(self, name: str) -> TableDetailResponse:
        self.described.append(name)
        column = ColumnInfo(
            name="id", type="bigint unsigned", nullable=False, default=None, key="PRI", extra=None
        )
        return TableDetailResponse(name=name, columns=[column], indexes=[], foreign_keys=[])


@pytest.fixture
def debug_client(client_factory) -> TestClient:
    client = client_factory(enable_debug_routes=True, service_api_key=SERVICE_KEY)
    client.app.state.schema_inspector = FakeInspector()
    return client


def test_absent_when_disabled(client: TestClient) -> None:
    assert client.get("/v1/debug/db/tables", headers=KEY_HEADER).status_code == 404


def test_requires_service_key(debug_client: TestClient, make_token: TokenFactory) -> None:
    assert debug_client.get("/v1/debug/db/tables").status_code == 401
    wrong = {"X-Service-Key": "x" * 40}
    assert debug_client.get("/v1/debug/db/tables", headers=wrong).status_code == 401
    # A valid *user* token is not enough.
    user = auth(make_token(RIDER_ID))
    assert debug_client.get("/v1/debug/db/tables", headers=user).status_code == 401


def test_list_tables(debug_client: TestClient) -> None:
    response = debug_client.get("/v1/debug/db/tables", headers=KEY_HEADER)
    assert response.status_code == 200
    assert response.json() == {"tables": [{"name": "users", "estimated_rows": 5, "engine": "InnoDB"}]}


def test_describe_table(debug_client: TestClient) -> None:
    response = debug_client.get("/v1/debug/db/tables/users", headers=KEY_HEADER)
    assert response.status_code == 200
    assert response.json()["columns"][0]["key"] == "PRI"


@pytest.mark.parametrize(
    "name",
    [
        "users;DROP TABLE users",
        "users' OR '1'='1",
        "users`",
        "information_schema.TABLES",
        "secret_table",
        "users%20--",
    ],
)
def test_table_name_injection_rejected(debug_client: TestClient, name: str) -> None:
    response = debug_client.get(f"/v1/debug/db/tables/{name}", headers=KEY_HEADER)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert debug_client.app.state.schema_inspector.described == []


async def test_real_inspector_rejects_bad_names_without_querying() -> None:
    class ExplodingEngine:
        def connect(self) -> None:
            raise AssertionError("must not touch the database")

    inspector = SchemaInspector(ExplodingEngine())  # type: ignore[arg-type]
    assert await inspector.describe_table("users;DROP TABLE users") is None
    assert await inspector.describe_table("a" * 65) is None
