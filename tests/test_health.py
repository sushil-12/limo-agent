"""Liveness and readiness probes."""

from __future__ import annotations

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import MODEL


def tags(*names: str) -> httpx.Response:
    return httpx.Response(200, json={"models": [{"name": n, "model": n} for n in names]})


def test_live(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]


def test_request_id_propagated(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": "trace-12345678"})
    assert response.headers["x-request-id"] == "trace-12345678"
    bad = client.get("/health/live", headers={"X-Request-ID": "bad id\r\n"})
    assert bad.headers["x-request-id"] != "bad id\r\n"


def test_ready_ok(client: TestClient, ollama: respx.MockRouter) -> None:
    ollama.get("/api/tags").mock(return_value=tags("llama3.2:latest", MODEL))
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["llm"] == {
        "status": "ok",
        "latency_ms": body["checks"]["llm"]["latency_ms"],
        "detail": None,
        "model_available": True,
    }


def test_ready_ollama_down(client: TestClient, ollama: respx.MockRouter) -> None:
    ollama.get("/api/tags").mock(side_effect=httpx.ConnectError("refused"))
    response = client.get("/health/ready")
    assert response.status_code == 503
    llm = response.json()["checks"]["llm"]
    assert llm["status"] == "down"
    assert llm["model_available"] is False
    assert llm["detail"] == "LLMConnectionError"
    assert response.json()["checks"]["database"]["status"] == "ok"


def test_ready_model_not_pulled(client: TestClient, ollama: respx.MockRouter) -> None:
    ollama.get("/api/tags").mock(return_value=tags("llama3.2:latest"))
    response = client.get("/health/ready")
    assert response.status_code == 503
    llm = response.json()["checks"]["llm"]
    assert llm["status"] == "down"
    assert llm["model_available"] is False
    assert llm["detail"] == f"model {MODEL} is not pulled"


def test_ready_database_down(client_factory, ollama: respx.MockRouter, tmp_path) -> None:
    client = client_factory()
    client.app.state.health_service._engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/missing/dir/x.db"
    )
    ollama.get("/api/tags").mock(return_value=tags(MODEL))
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["database"]["status"] == "down"


def test_ready_hides_details_in_production(client_factory, ollama: respx.MockRouter) -> None:
    client = client_factory(app_env="production")
    ollama.get("/api/tags").mock(side_effect=httpx.ConnectError("refused"))
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["llm"]["detail"] is None


def test_docs_hidden_in_production(client_factory) -> None:
    assert client_factory().get("/openapi.json").status_code == 200
    prod = client_factory(app_env="production")
    assert prod.get("/openapi.json").status_code == 404
    assert prod.get("/docs").status_code == 404


def test_unknown_route_uses_error_schema(client: TestClient) -> None:
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_cors_preflight(client: TestClient) -> None:
    response = client.options(
        "/v1/chat",
        headers={
            "Origin": "http://localhost:4200",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:4200"
    assert "access-control-allow-credentials" not in response.headers

    evil = client.options(
        "/v1/chat",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in evil.headers
