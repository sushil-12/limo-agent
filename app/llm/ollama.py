"""Ollama provider using the native /api/chat endpoint (NDJSON streaming).

Docs: https://github.com/ollama/ollama/blob/main/docs/api.md
Durations in Ollama responses are nanoseconds.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import httpx
import orjson

from app.llm.base import (
    ChatResult,
    LLMConnectionError,
    LLMError,
    LLMMessage,
    LLMModelNotFoundError,
    LLMResponseError,
    LLMTimeoutError,
    ProviderHealth,
    StreamDelta,
    StreamFinal,
    StreamItem,
    Timings,
    Usage,
)

CHAT_PATH = "/api/chat"
TAGS_PATH = "/api/tags"
WARMUP_READ_TIMEOUT = 600.0  # loading a large model from disk can take minutes
_NS_PER_MS = 1_000_000
_JSON_HEADERS = {"Content-Type": "application/json"}


class OllamaProvider:
    """LLMProvider backed by a local Ollama server."""

    name = "ollama"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        model: str,
        keep_alive: str,
        num_ctx: int,
        num_predict: int,
        temperature: float,
    ) -> None:
        self._client = client
        self.model = model
        self._keep_alive = keep_alive
        self._options = {"num_ctx": num_ctx, "num_predict": num_predict, "temperature": temperature}

    def _payload(
        self, messages: Sequence[LLMMessage], *, stream: bool, overrides: dict[str, Any] | None = None
    ) -> bytes:
        # num_ctx is never overridden: changing it makes Ollama reload the model.
        return orjson.dumps(
            {
                "model": self.model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "stream": stream,
                "keep_alive": self._keep_alive,
                "options": {**self._options, **(overrides or {})},
            }
        )

    async def chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        """Generate a full reply with stream=false."""
        overrides: dict[str, Any] = {}
        if max_tokens is not None:
            overrides["num_predict"] = max_tokens
        if temperature is not None:
            overrides["temperature"] = temperature
        started = time.perf_counter()
        with _translate_errors():
            response = await self._client.post(
                CHAT_PATH,
                content=self._payload(messages, stream=False, overrides=overrides),
                headers=_JSON_HEADERS,
            )
        _raise_for_status(response.status_code, response.content)
        data = _parse(response.content)
        content = (data.get("message") or {}).get("content", "")
        usage, timings = _stats(data, started, first_token_at=None)
        return ChatResult(content, data.get("model", self.model), usage, timings, data.get("done_reason"))

    async def stream_chat(self, messages: Sequence[LLMMessage]) -> AsyncGenerator[StreamItem, None]:
        """Stream NDJSON chunks as StreamDelta items, ending with StreamFinal.

        Leaving the ``async with`` (normally, on error, or because the consumer closed
        this generator after a client disconnect) closes the HTTP stream, which makes
        Ollama stop generating.
        """
        started = time.perf_counter()
        first_token_at: float | None = None
        with _translate_errors():
            async with self._client.stream(
                "POST", CHAT_PATH, content=self._payload(messages, stream=True), headers=_JSON_HEADERS
            ) as response:
                if response.status_code != httpx.codes.OK:
                    _raise_for_status(response.status_code, await response.aread())
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    data = _parse(line.encode())
                    content = (data.get("message") or {}).get("content", "")
                    if content:
                        if first_token_at is None:
                            first_token_at = time.perf_counter()
                        yield StreamDelta(content)
                    if data.get("done"):
                        usage, timings = _stats(data, started, first_token_at)
                        yield StreamFinal(
                            data.get("model", self.model), usage, timings, data.get("done_reason")
                        )
                        return
        raise LLMResponseError("stream ended without a final message")

    async def list_models(self) -> list[str]:
        """Names of locally pulled models (GET /api/tags)."""
        with _translate_errors():
            response = await self._client.get(TAGS_PATH)
        _raise_for_status(response.status_code, response.content)
        return _model_names(_parse(response.content))

    async def health(self, max_seconds: float) -> ProviderHealth:
        """Check /api/tags and whether the configured model has been pulled."""
        started = time.perf_counter()
        try:
            with _translate_errors():
                response = await self._client.get(TAGS_PATH, timeout=max_seconds)
            _raise_for_status(response.status_code, response.content)
            names = _model_names(_parse(response.content))
        except LLMError as exc:
            return ProviderHealth(False, _ms_since(started), self.model, False, type(exc).__name__)
        available = model_matches(self.model, names)
        return ProviderHealth(True, _ms_since(started), self.model, available)

    async def warmup(self) -> None:
        """Load the model (empty messages + keep_alive, per the Ollama docs).

        The same options are sent as real requests: Ollama reloads the model when
        ``num_ctx`` changes, which would throw the warm-up away.
        """
        payload = orjson.dumps(
            {"model": self.model, "messages": [], "keep_alive": self._keep_alive, "options": self._options}
        )
        timeout = httpx.Timeout(self._client.timeout.connect, read=WARMUP_READ_TIMEOUT)
        with _translate_errors():
            response = await self._client.post(
                CHAT_PATH, content=payload, headers=_JSON_HEADERS, timeout=timeout
            )
        _raise_for_status(response.status_code, response.content)


def model_matches(configured: str, available: Sequence[str]) -> bool:
    """True if ``configured`` is pulled; a missing tag means ``:latest``."""
    wanted = configured if ":" in configured else f"{configured}:latest"
    return wanted in available


@contextmanager
def _translate_errors() -> Iterator[None]:
    """Map httpx exceptions to provider-neutral LLM errors."""
    try:
        yield
    except httpx.TimeoutException as exc:
        if isinstance(exc, httpx.ConnectTimeout):
            raise LLMConnectionError("connect timeout") from exc
        raise LLMTimeoutError(type(exc).__name__) from exc
    except httpx.HTTPError as exc:
        raise LLMConnectionError(type(exc).__name__) from exc


def _raise_for_status(status: int, body: bytes) -> None:
    if status == httpx.codes.OK:
        return
    try:
        detail = str(orjson.loads(body).get("error", ""))[:300]
    except (orjson.JSONDecodeError, AttributeError):
        detail = ""
    if status == httpx.codes.NOT_FOUND:
        raise LLMModelNotFoundError(detail or "model not found")
    raise LLMResponseError(f"HTTP {status}: {detail}")


def _parse(raw: bytes) -> dict[str, Any]:
    try:
        data = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        raise LLMResponseError("invalid JSON from Ollama") from exc
    if not isinstance(data, dict):
        raise LLMResponseError("unexpected payload from Ollama")
    if data.get("error"):
        # Ollama reports mid-stream failures as an {"error": "..."} line.
        raise LLMResponseError(str(data["error"])[:300])
    return data


def _model_names(data: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in data.get("models") or []:
        for key in ("name", "model"):
            value = item.get(key)
            if isinstance(value, str) and value not in names:
                names.append(value)
    return names


def _stats(data: dict[str, Any], started: float, first_token_at: float | None) -> tuple[Usage, Timings]:
    load_ns = data.get("load_duration")
    prompt_ns = data.get("prompt_eval_duration")
    eval_ns = data.get("eval_duration")
    eval_count = int(data.get("eval_count") or 0)

    if first_token_at is not None:
        ttft: float | None = round((first_token_at - started) * 1000, 1)
    elif load_ns is not None or prompt_ns is not None:
        ttft = round(((load_ns or 0) + (prompt_ns or 0)) / _NS_PER_MS, 1)
    else:
        ttft = None

    usage = Usage(int(data.get("prompt_eval_count") or 0), eval_count)
    timings = Timings(
        total_ms=_ms_since(started),
        load_ms=round(load_ns / _NS_PER_MS, 1) if load_ns is not None else None,
        time_to_first_token_ms=ttft,
        tokens_per_sec=round(eval_count / (eval_ns / 1e9), 2) if eval_ns else None,
    )
    return usage, timings


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
