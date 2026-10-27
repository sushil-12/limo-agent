"""Server-Sent Events encoding with keep-alive pings."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import anyio
import orjson

from app.services.chat_service import (
    ChatBooking,
    ChatData,
    ChatDelta,
    ChatDone,
    ChatEvent,
    ChatFailed,
    ChatStarted,
)

PING = b": ping\n\n"
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",  # stop Nginx from buffering the stream
}


def format_event(event: str, data: dict[str, Any]) -> bytes:
    """Encode one SSE event. orjson output never contains raw newlines."""
    return b"event: " + event.encode() + b"\ndata: " + orjson.dumps(data) + b"\n\n"


def encode_chat_event(event: ChatEvent) -> bytes:
    """Map a service-level chat event to its SSE frame."""
    match event:
        case ChatStarted(conversation_id=cid, model=model):
            return format_event("start", {"conversation_id": cid, "model": model})
        case ChatData(data=data):
            return format_event("data", data.model_dump())
        case ChatBooking(booking=booking):
            return format_event("booking", booking.model_dump())
        case ChatDelta(content=content):
            return format_event("delta", {"content": content})
        case ChatDone(usage=usage, timings=timings, reply_type=reply_type):
            return format_event(
                "done",
                {"usage": usage.model_dump(), "timings": timings.model_dump(), "reply_type": reply_type},
            )
        case ChatFailed(code=code, message=message):
            return format_event("error", {"code": code, "message": message})


async def with_keepalive[T](
    source: AsyncGenerator[T, None], interval: float
) -> AsyncGenerator[T | None, None]:
    """Re-yield items from ``source``, yielding ``None`` after ``interval`` idle seconds.

    The next item is awaited in a separate task so a timeout does not cancel it
    (``asyncio.wait`` leaves pending tasks running). When this generator is closed
    or cancelled (client disconnected), the in-flight task is cancelled and the
    source is closed, which closes the upstream LLM stream.
    """
    pending: asyncio.Task[T] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(source))
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield None
                continue
            task, pending = pending, None
            try:
                item = task.result()
            except StopAsyncIteration:
                return
            yield item
    finally:
        # Starlette cancels via an anyio scope that re-cancels every await, so the
        # cleanup is shielded to guarantee the upstream stream really gets closed.
        with anyio.CancelScope(shield=True):
            if pending is not None:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                    await pending
            await source.aclose()


async def chat_sse(events: AsyncGenerator[ChatEvent, None], ping_interval: float) -> AsyncIterator[bytes]:
    """Encode chat events as SSE frames, with ``: ping`` comments while idle."""
    async with contextlib.aclosing(with_keepalive(events, ping_interval)) as stream:
        async for event in stream:
            yield PING if event is None else encode_chat_event(event)
