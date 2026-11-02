"""The OpenAI provider (app.llm.openai) and the masking of personal details (app.llm.redact)."""

from __future__ import annotations

import httpx
import orjson
import pytest
import respx

from app.llm.base import LLMMessage, LLMModelNotFoundError, StreamDelta, StreamFinal
from app.llm.openai import OpenAIProvider
from app.llm.redact import CARD, EMAIL, PHONE, redact

BASE = "https://api.openai.test/v1/"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("call +1 (312) 555-0199 now", f"call {PHONE} now"),
        ("phone 9876543210", f"phone {PHONE}"),
        ("312.555.0199", PHONE),
        ("+91 98765 43210", PHONE),
        ("card 4242 4242 4242 4242 exp", f"card {CARD} exp"),
        ("4242-4242-4242-4242", CARD),
        ("mail a.b+x@gmail.com", f"mail {EMAIL}"),
    ],
)
def test_personal_details_are_masked(text: str, expected: str) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "on 2026-11-14 17:00 from 10000 W O'Hare Ave, Chicago, IL 60666",
        "12/11/2026 5pm",
        "lat 41.878113 -87.629799",
        "booking 2096 total $1,234.56",
        "flight AA 1234, 3 passengers, 2 bags",
        "Passenger name? Asha Rao",
    ],
)
def test_trip_details_are_kept(text: str) -> None:
    assert redact(text) == text


def _provider(client: httpx.AsyncClient) -> OpenAIProvider:
    return OpenAIProvider(client, model="gpt-test", max_tokens=100, temperature=0.3)


@respx.mock
async def test_chat_sends_masked_messages_and_reads_the_reply() -> None:
    route = respx.post(f"{BASE}chat/completions").respond(
        json={
            "model": "gpt-test-2026",
            "choices": [{"message": {"content": "Hi Asha"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }
    )
    async with httpx.AsyncClient(base_url=BASE) as client:
        result = await _provider(client).chat(
            [LLMMessage("user", "I'm Asha, my phone is 312-555-0199")], max_tokens=20, temperature=0.0
        )
    sent = orjson.loads(route.calls[0].request.content)
    assert sent["messages"] == [{"role": "user", "content": f"I'm Asha, my phone is {PHONE}"}]
    assert (sent["max_completion_tokens"], sent["temperature"]) == (20, 0.0)
    assert (result.content, result.model, result.done_reason) == ("Hi Asha", "gpt-test-2026", "stop")
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (12, 3)


@respx.mock
async def test_stream_yields_deltas_then_usage() -> None:
    chunks = [
        {"model": "gpt-test", "choices": [{"delta": {"content": "Hel"}}]},
        {"model": "gpt-test", "choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
        {"model": "gpt-test", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    ]
    sse = "".join(f"data: {orjson.dumps(c).decode()}\n\n" for c in chunks) + "data: [DONE]\n\n"
    respx.post(f"{BASE}chat/completions").respond(text=sse, headers={"Content-Type": "text/event-stream"})
    async with httpx.AsyncClient(base_url=BASE) as client:
        items = [item async for item in _provider(client).stream_chat([LLMMessage("user", "hi")])]
    assert [i.content for i in items if isinstance(i, StreamDelta)] == ["Hel", "lo"]
    final = items[-1]
    assert isinstance(final, StreamFinal)
    assert (final.usage.completion_tokens, final.done_reason) == (2, "stop")


@respx.mock
async def test_unknown_model_and_health() -> None:
    respx.post(f"{BASE}chat/completions").respond(
        404, json={"error": {"message": "The model does not exist", "code": "model_not_found"}}
    )
    respx.get(f"{BASE}models/gpt-test").respond(404, json={"error": {"code": "model_not_found"}})
    async with httpx.AsyncClient(base_url=BASE) as client:
        provider = _provider(client)
        with pytest.raises(LLMModelNotFoundError):
            await provider.chat([LLMMessage("user", "hi")])
        health = await provider.health(1.0)
    assert (health.ok, health.model_available) == (True, False)
