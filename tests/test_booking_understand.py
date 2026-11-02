"""The model reading what the trip rules miss (app.services.booking.understand)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date

import orjson
import pytest

from app.llm.base import ChatResult, LLMConnectionError, LLMMessage, Timings, Usage
from app.services.booking.parse import ParsedTrip, parse_trip
from app.services.booking.understand import TripReader, needs_reading, read_answer
from tests.test_booking import make_flow, run, to_vehicle_menu

TODAY = date(2026, 10, 26)


@pytest.fixture
def out() -> list[str]:
    return []


class FakeModel:
    name = "fake"
    model = "fake"

    def __init__(self, answer: dict | str | Exception, delay: float = 0.0) -> None:
        self.answer, self.delay = answer, delay
        self.asked: list[str] = []

    async def chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        self.asked.append(messages[-1].content)
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.answer, Exception):
            raise self.answer
        content = self.answer if isinstance(self.answer, str) else orjson.dumps(self.answer).decode()
        return ChatResult(content, "fake", Usage(0, 0), Timings(0.0, None, None, None))


def answer(**fields: object) -> dict:
    base = dict.fromkeys(("pickup", "dropoff", "date", "time", "service", "hours", "pax", "luggage"))
    return {**base, **fields}


async def improve(text: str, model: FakeModel, seconds: float = 8.0) -> ParsedTrip:
    reader = TripReader(model, timeout_seconds=seconds)  # type: ignore[arg-type]
    return await reader.improve(text, parse_trip(text, TODAY), TODAY)


def test_only_asks_when_the_rules_miss_a_place() -> None:
    def needs(text: str) -> bool:
        return needs_reading(text, parse_trip(text, TODAY))

    assert not needs("5 am pickup on 14 nov from ord to mdw")
    assert not needs("help me create a booking")
    assert not needs("Book a ride")
    assert needs("need a car, ORD, going downtown with my wife at 5pm")
    assert needs("from ord with 2 people at about 5 in the evening")


async def test_fills_the_places_the_rules_could_not_split() -> None:
    text = "pickup me at ord terminal 3 and take us downtown to 233 S Wacker Dr, 2 people, 14 nov 5 pm"
    model = FakeModel(answer(pickup="ord terminal 3", dropoff="233 S Wacker Dr", pax=2, date="2026-11-14"))
    parsed = await improve(text, model)
    assert (parsed.pickup, parsed.dropoff) == ("ord terminal 3", "233 S Wacker Dr")
    assert parsed.pax == 2
    assert parsed.date == date(2026, 11, 14)
    assert parsed.time is not None
    assert parsed.time.value() == "17:00:00"  # the rules' answer stands
    assert model.asked == [text]


async def test_drops_anything_the_user_did_not_say() -> None:
    text = "need a car, ORD, going downtown"
    model = FakeModel(
        answer(pickup="ORD", dropoff="233 S Wacker Dr", pax=4, luggage=2, date="2026-11-01", time="17:00")
    )
    parsed = await improve(text, model)
    assert parsed.pickup == "ORD"
    assert parsed.dropoff is None  # an address that was never typed
    assert (parsed.pax, parsed.luggage, parsed.date, parsed.time) == (None, None, None, None)


def test_checks_each_value() -> None:
    text = "ord to the hilton with two people and no bags on dec 5 at 9"
    found = read_answer(
        "Sure! "
        + orjson.dumps(
            answer(pickup="ord", dropoff="the hilton", pax=2, luggage=3, date="2026-12-05", time="09:00")
        ).decode(),
        text,
        TODAY,
    )
    assert (found.pickup, found.dropoff) == ("ord", "the hilton")
    assert found.pax == 2  # "two"
    assert found.luggage == 0  # "no bags", whatever the model said
    assert found.date == date(2026, 12, 5)
    assert found.time is not None
    assert found.time.ambiguous  # "at 9": morning or evening is asked
    assert read_answer("not json", text, TODAY).pickup is None
    past = read_answer(orjson.dumps(answer(date="2025-01-01")).decode(), text, TODAY)
    assert past.date is None


@pytest.mark.parametrize("failure", [LLMConnectionError("down"), "timeout"])
async def test_a_failing_model_leaves_the_rules_answer(failure: object) -> None:
    text = "need a car, ORD, going downtown"
    model = (
        FakeModel(answer(pickup="ORD"), delay=1.0) if failure == "timeout" else FakeModel(failure)  # type: ignore[arg-type]
    )
    parsed = await improve(text, model, seconds=0.05)
    assert parsed == parse_trip(text, TODAY)


async def test_the_flow_skips_questions_the_model_answered(out: list[str]) -> None:
    flow, _ = make_flow("individual", out)
    flow.reader = TripReader(
        FakeModel(answer(pickup="ORD", dropoff="MDW", pax=2, luggage=4)),  # type: ignore[arg-type]
    )
    await flow.start("hey get me from ORD over to MDW, 2 of us, 4 suitcases, 14 nov 5 pm")
    assert flow.d.outbound.pickup is not None
    assert flow.d.outbound.dropoff is not None
    assert (flow.d.pax, flow.d.luggage) == (2, 4)
    assert flow.d.outbound.date == date(2026, 11, 14)


def test_a_time_needs_its_hour_typed() -> None:
    def time_of(text: str, value: str) -> str | None:
        found = read_answer(orjson.dumps(answer(time=value)).decode(), text, TODAY).time
        return None if found is None else found.value()

    assert time_of("1.", "09:00") is None  # the example's time, not the user's
    assert time_of("2 pax", "09:00") is None
    assert time_of("pickup at 9", "09:00") == "09:00:00"
    assert time_of("pickup 21:30", "21:30") == "21:30:00"
    assert time_of("9 pm please", "21:00") == "21:00:00"
    assert time_of("at 12 noon", "12:00") == "12:00:00"


async def test_a_menu_number_with_a_dot_is_a_pick_not_a_trip(out: list[str]) -> None:
    flow, _ = await to_vehicle_menu("admin", out)
    model = FakeModel(answer(time="09:00"))  # what llama3.2 made of "1."
    flow.reader = TripReader(model)  # type: ignore[arg-type]
    assert await run(flow, "1") == ["customer_kind"]
    assert await run(flow, "1.") == ["customer_search"]
    assert model.asked == []
    assert flow.d.outbound.time == "05:00:00"


async def test_a_sentence_left_in_a_place_is_asked_for_again() -> None:
    text = "my family of 4 lands at midway around 7 in the evening, we need to get to navy pier"
    assert parse_trip(text, TODAY).pickup is not None  # the rules keep most of the sentence
    parsed = await improve(text, FakeModel(answer(pickup="ORD", dropoff="navy pier", pax=4)))
    assert parsed.pickup is None  # "ORD" was never typed, and the sentence is not a place
    assert parsed.dropoff == "navy pier"
    assert parsed.pax == 4
