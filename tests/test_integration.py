"""End-to-end check against a real Ollama and the real Laravel MySQL database.

Skipped by default. Run with ``make test-integration`` (``pytest -m integration``)
after filling in .env. Set INTEGRATION_BEARER_TOKEN to a real Passport token of a
rider or driver to also exercise /v1/me and /v1/chat.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

pytestmark = pytest.mark.integration


def test_real_dependencies() -> None:
    settings = Settings(ollama_warmup=False)  # type: ignore[call-arg]  # reads .env
    with TestClient(create_app(settings)) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 200, ready.json()

        token = os.environ.get("INTEGRATION_BEARER_TOKEN")
        if not token:
            pytest.skip("INTEGRATION_BEARER_TOKEN not set; skipping auth + chat")
        headers = {"Authorization": f"Bearer {token}"}

        me = client.get("/v1/me", headers=headers)
        assert me.status_code == 200, me.json()

        reply = client.post(
            "/v1/chat",
            headers=headers,
            json={"messages": [{"role": "user", "content": "Say hello in five words."}], "stream": False},
        )
        assert reply.status_code == 200, reply.json()
        assert reply.json()["content"]
