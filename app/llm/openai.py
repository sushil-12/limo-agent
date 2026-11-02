"""OpenAI provider using the Chat Completions endpoint (SSE streaming).

Docs: https://platform.openai.com/docs/api-reference/chat
Every message is passed through ``redact`` first: card numbers, phone numbers and emails
never leave this server.
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
from app.llm.redact import redact

CHAT_PATH = "chat/completions"
MODELS_PATH = "models"
_JSON_HEADERS = {"Content-Type": "application/json"}


class OpenAIProvider:
    """LLMProvider backed by the OpenAI API."""

    name = "openai"

    def __init__(self, client: httpx.AsyncClient, *, model: str, max_tokens: int, temperature: float) -> None:
        self._client = client
        self.model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    def _payload(
        self,
        messages: Sequence[LLMMessage],
        *,
        stream: bool,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> bytes:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": redact(m.content)} for m in messages],
            "max_completion_tokens": max_tokens if max_tokens is not None else self._max_tokens,
            "temperature": temperature if temperature is not None else self._temperature,
        }
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        return orjson.dumps(payload)

    async def chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        """Generate a full reply."""
        started = time.perf_counter()
        with _translate_errors():
            response = await self._client.post(
                CHAT_PATH,
                content=self._payload(messages, stream=False, max_tokens=max_tokens, temperature=temperature),
                headers=_JSON_HEADERS,
            )
        _raise_for_status(response.status_code, response.content)
        data = _parse(response.content)
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        return ChatResult(
            content,
            data.get("model", self.model),
            _usage(data),
            Timings(_ms_since(started), None, None, None),
            choice.get("finish_reason"),
        )

    async def stream_chat(self, messages: Sequence[LLMMessage]) -> AsyncGenerator[StreamItem, None]:
        """Stream SSE chunks as StreamDelta items, ending with StreamFinal.

        Leaving the ``async with`` closes the HTTP stream, which stops the generation.
        """
        started = time.perf_counter()
        first_token_at: float | None = None
        model, usage, reason = self.model, Usage(0, 0), None
        with _translate_errors():
            async with self._client.stream(
                "POST", CHAT_PATH, content=self._payload(messages, stream=True), headers=_JSON_HEADERS
            ) as response:
                if response.status_code != httpx.codes.OK:
                    _raise_for_status(response.status_code, await response.aread())
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        ttft = round((first_token_at - started) * 1000, 1) if first_token_at else None
                        yield StreamFinal(model, usage, Timings(_ms_since(started), None, ttft, None), reason)
                        return
                    data = _parse(body.encode())
                    model = data.get("model", model)
                    if data.get("usage"):  # the last chunk, with include_usage
                        usage = _usage(data)
                    for choice in data.get("choices") or []:
                        reason = choice.get("finish_reason") or reason
                        content = (choice.get("delta") or {}).get("content")
                        if content:
                            if first_token_at is None:
                                first_token_at = time.perf_counter()
                            yield StreamDelta(content)
        raise LLMResponseError("stream ended without [DONE]")

    async def list_models(self) -> list[str]:
        """Model ids this API key can use."""
        with _translate_errors():
            response = await self._client.get(MODELS_PATH)
        _raise_for_status(response.status_code, response.content)
        return [str(m.get("id")) for m in _parse(response.content).get("data") or [] if m.get("id")]

    async def health(self, max_seconds: float) -> ProviderHealth:
        """Check that the API answers and the configured model exists for this key."""
        started = time.perf_counter()
        try:
            with _translate_errors():
                response = await self._client.get(f"{MODELS_PATH}/{self.model}", timeout=max_seconds)
            _raise_for_status(response.status_code, response.content)
        except LLMModelNotFoundError:
            return ProviderHealth(True, _ms_since(started), self.model, False)
        except LLMError as exc:
            return ProviderHealth(False, _ms_since(started), self.model, False, type(exc).__name__)
        return ProviderHealth(True, _ms_since(started), self.model, True)

    async def warmup(self) -> None:
        """Nothing to load: the model is hosted."""


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
        error = orjson.loads(body).get("error") or {}
        detail, code = str(error.get("message", ""))[:300], error.get("code")
    except (orjson.JSONDecodeError, AttributeError):
        detail, code = "", None
    if status == httpx.codes.NOT_FOUND or code == "model_not_found":
        raise LLMModelNotFoundError(detail or "model not found")
    raise LLMResponseError(f"HTTP {status}: {detail}")


def _parse(raw: bytes) -> dict[str, Any]:
    try:
        data = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        raise LLMResponseError("invalid JSON from OpenAI") from exc
    if not isinstance(data, dict):
        raise LLMResponseError("unexpected payload from OpenAI")
    if data.get("error"):
        raise LLMResponseError(str(data["error"])[:300])
    return data


def _usage(data: dict[str, Any]) -> Usage:
    usage = data.get("usage") or {}
    return Usage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
