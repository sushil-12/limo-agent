"""Builds the configured LLM provider and its HTTP client."""

from __future__ import annotations

import httpx

from app.core.config import Settings
from app.llm.base import LLMProvider
from app.llm.ollama import OllamaProvider
from app.llm.openai import OpenAIProvider


def create_llm_client(settings: Settings) -> httpx.AsyncClient:
    """The HTTP client for the configured provider (LLM_PROVIDER)."""
    if settings.llm_provider == "openai":
        return create_openai_client(settings)
    return create_ollama_client(settings)


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


def create_openai_client(settings: Settings) -> httpx.AsyncClient:
    """A pooled client carrying the API key; requests use paths relative to OPENAI_BASE_URL."""
    if settings.openai_api_key is None:
        raise ValueError("OPENAI_API_KEY is not set")
    return httpx.AsyncClient(
        base_url=settings.openai_base_url.rstrip("/") + "/",
        headers={"Authorization": f"Bearer {settings.openai_api_key.get_secret_value()}"},
        timeout=httpx.Timeout(connect=5.0, read=settings.openai_timeout, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=60.0),
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
    if name == "openai":
        return OpenAIProvider(
            client,
            model=model or settings.openai_model,
            max_tokens=settings.ollama_num_predict,
            temperature=settings.openai_temperature,
        )
    raise ValueError(f"Unknown LLM provider: {name!r}")
