"""Provider-neutral LLM types and the LLMProvider protocol.

Ollama implements this today. An OpenAI provider (tool calling) plugs in later by
implementing the same protocol and registering in ``app.llm.factory``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """One chat message sent to a provider."""

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts reported by the provider."""

    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True, slots=True)
class Timings:
    """Latency figures for one generation. Durations in milliseconds."""

    total_ms: float
    load_ms: float | None
    time_to_first_token_ms: float | None
    tokens_per_sec: float | None


@dataclass(frozen=True, slots=True)
class ChatResult:
    """A complete (non-streamed) reply."""

    content: str
    model: str
    usage: Usage
    timings: Timings
    done_reason: str | None = None


@dataclass(frozen=True, slots=True)
class StreamDelta:
    """A chunk of streamed assistant text."""

    content: str


@dataclass(frozen=True, slots=True)
class StreamFinal:
    """The last stream item, carrying usage and timings."""

    model: str
    usage: Usage
    timings: Timings
    done_reason: str | None = None


StreamItem = StreamDelta | StreamFinal


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Reachability of the provider and whether the configured model is available."""

    ok: bool
    latency_ms: float
    model: str
    model_available: bool
    error: str | None = None


class LLMError(Exception):
    """Base class for provider failures. Messages are for logs, not for clients."""


class LLMConnectionError(LLMError):
    """The provider could not be reached or the connection dropped."""


class LLMTimeoutError(LLMError):
    """The provider did not respond in time."""


class LLMResponseError(LLMError):
    """The provider answered with an error or an unexpected payload."""


class LLMModelNotFoundError(LLMResponseError):
    """The configured model is not available on the provider."""


class LLMProvider(Protocol):
    """Interface every LLM backend implements."""

    name: str
    model: str

    async def chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        """Generate a complete reply. ``max_tokens``/``temperature`` override the defaults."""
        ...

    def stream_chat(self, messages: Sequence[LLMMessage]) -> AsyncGenerator[StreamItem, None]:
        """Stream a reply: zero or more StreamDelta items, then one StreamFinal.

        Closing the iterator early must release the upstream connection.
        """
        ...

    async def health(self, max_seconds: float) -> ProviderHealth:
        """Check reachability and model availability. Never raises."""
        ...

    async def list_models(self) -> list[str]:
        """Return the model names available on the provider."""
        ...

    async def warmup(self) -> None:
        """Load the model into memory so the first real request is fast."""
        ...
