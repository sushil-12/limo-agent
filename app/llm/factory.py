"""Builds the configured LLM provider and its HTTP client."""

from __future__ import annotations

import httpx

from app.core.config import Settings
from app.llm.base import LLMProvider
from app.llm.ollama import OllamaProvider


def create_ollama_client(settings: Settings) -> httpx.AsyncClient:
    """One pooled client for the whole process, with separate connect/read timeouts."""
    return httpx.AsyncClient(
        base_url=settings.ollama_base_url,
        timeout=httpx.Timeout(
            connect=settings.ollama_connect_timeout,
            read=settings.ollama_timeout,
            write=10.0,
            pool=5.0,
        ),
        limits=httpx.Limits(
            max_connections=settings.ollama_max_connections,
            max_keepalive_connections=settings.ollama_max_connections,
            keepalive_expiry=60.0,
        ),
    )


def create_llm_provider(
    name: str, settings: Settings, client: httpx.AsyncClient, *, model: str | None = None
) -> LLMProvider:
    """Return the provider called ``name``; ``model`` overrides the configured model."""
    if name == "ollama":
        return OllamaProvider(
            client,
            model=model or settings.ollama_model,
            keep_alive=settings.ollama_keep_alive,
            num_ctx=settings.ollama_num_ctx,
            num_predict=settings.ollama_num_predict,
            temperature=settings.ollama_temperature,
        )
    raise ValueError(f"Unknown LLM provider: {name!r}")
