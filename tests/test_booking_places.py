"""Places once both ends are known: countries, a driving route, airport terminals (booking.ts)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services.booking.maps import PlaceDetails, Suggestion
from tests.test_booking import PLACES, FakeMaps, make_flow

DELHI = PlaceDetails(
    place_id="p-delhi",
    name="New Delhi",
    formatted_address="New Delhi, Delhi, India",
    lat=28.6139,
    lng=77.2090,
    types=("locality",),
    country_names=("New Delhi", "India"),
    country="India",
)
TERMINAL_5 = PlaceDetails(
    place_id="p-ord-t5",
    name="ORD Terminal 5",
    formatted_address="ORD Terminal 5, Chicago, IL 60666, USA",
    lat=41.9742,
    lng=-87.8906,
    types=("establishment",),
    country_names=("Chicago", "United States"),
    country="United States",
)
ORD_TERMINALS = [
    Suggestion("p-ord-t1", "ORD Terminal 1, Chicago, IL, USA", ("establishment",)),
    Suggestion("p-ord-t5", "ORD Terminal 5, Chicago, IL, USA", ("establishment",)),
    Suggestion("p-ord-t5-park", "Terminal 5 Parking Garage, Chicago, IL, USA", ("parking",)),
    Suggestion("p-street", "Terminal St, Chicago, IL, USA", ("route",)),  # a street, never a terminal
]


@dataclass
class PlacesMaps(FakeMaps):
    async def autocomplete(self, text: str, *, near: tuple[float, float] | None = None) -> list[Suggestion]:
        if near is None and "delhi" in text.lower():
            return [Suggestion("p-delhi", "New Delhi, Delhi, India", ("locality",))]
        if near is None and "o'hare terminal" in text.lower():
            return [Suggestion("p-ord-t5", "ORD Terminal 5, Chicago, IL, USA", ("establishment",))]
        return await super().autocomplete(text, near=near)

    async def details(self, place_id: str) -> PlaceDetails:
        extra = {p.place_id: p for p in (DELHI, TERMINAL_5)}
        return extra.get(place_id) or next(p for p in PLACES.values() if p.place_id == place_id)


def flow_with(out: list[str], maps: PlacesMaps):  # type: ignore[no-untyped-def]
    flow, api = make_flow("individual", out)
    flow.maps = maps  # type: ignore[assignment]
    return flow, api


@pytest.fixture
def out() -> list[str]:
    return []


async def test_pickup_and_drop_off_in_different_countries_are_refused(out: list[str]) -> None:
    flow, _ = flow_with(out, PlacesMaps())
    await flow.start("book a ride from new delhi to ord on 14 nov at 5 pm")
    assert flow.d.outbound.pickup is not None
    assert flow.d.outbound.dropoff is None  # never accepted
    assert flow.pending == "dropoff"
    said = "\n".join(out)
    assert "is in United States, but the pickup (New Delhi - New Delhi, Delhi, India) is in India" in said
    assert "Please select a valid location point." in said
    assert "Where is the drop-off? (in India, or 'change pickup to ...')" in said


async def test_no_driving_route_asks_again_before_the_other_questions(out: list[str]) -> None:
    flow, _ = flow_with(out, PlacesMaps(no_route=True))
    await flow.start("book a ride from ord to mdw")
    assert flow.d.outbound.dropoff is None
    assert flow.pending == "dropoff"  # not the date, passengers or bags first
    assert "There is no driving route from" in "\n".join(out)


async def test_a_reachable_route_is_checked_once(out: list[str]) -> None:
    maps = PlacesMaps()
    flow, _ = flow_with(out, maps)
    await flow.start("book a ride from ord to 233 s wacker dr")
    await flow.handle("14 nov")
    await flow.handle("5 pm")
    assert len(maps.route_calls) == 1


async def test_an_airport_offers_its_terminals(out: list[str]) -> None:
    flow, _ = flow_with(out, PlacesMaps(terminals=ORD_TERMINALS))
    await flow.start("book a ride from ord to 233 s wacker dr")
    assert flow.menu is not None
    assert flow.menu.kind == "terminal:pickup"
    assert out[-3:] == [
        "  1. O'Hare International Airport (terminal not known)",
        "  2. Terminal 1",
        "  3. Terminal 5",
    ]
    await flow.handle("3")
    pickup = flow.d.outbound.pickup
    assert pickup is not None
    assert (pickup.kind, pickup.airport_id) == ("airport", 2)  # still ORD for the API
    assert pickup.short.startswith("ORD Terminal 5")
    assert (pickup.lat, pickup.lng) == (TERMINAL_5.lat, TERMINAL_5.lng)
    assert flow.menu is None
    assert flow.pending == "date"  # asked once, then the booking goes on


async def test_terminal_can_be_typed_or_skipped(out: list[str]) -> None:
    flow, _ = flow_with(out, PlacesMaps(terminals=ORD_TERMINALS))
    await flow.start("book a ride from ord to 233 s wacker dr")
    await flow.handle("t5")
    assert flow.d.outbound.pickup is not None
    assert flow.d.outbound.pickup.short.startswith("ORD Terminal 5")

    other, _ = flow_with(out, PlacesMaps(terminals=ORD_TERMINALS))
    await other.start("book a ride from ord to 233 s wacker dr")
    await other.handle("1")  # terminal not known: the airport stays
    assert other.d.outbound.pickup is not None
    assert other.d.outbound.pickup.short.startswith("O'Hare International Airport")
    assert other.pending == "date"


async def test_an_airport_without_terminals_is_not_interrupted(out: list[str]) -> None:
    flow, _ = flow_with(out, PlacesMaps())
    await flow.start("book a ride from ord to 233 s wacker dr")
    assert flow.menu is None
    assert flow.pending == "date"


async def test_code_with_terminal_is_an_airport(out: list[str]) -> None:
    flow, _ = flow_with(out, PlacesMaps())
    await flow.start("book a ride from mdw terminal to 233 s wacker dr")
    pickup = flow.d.outbound.pickup
    assert pickup is not None
    assert pickup.kind == "airport"
    assert flow.d.outbound.transfer_type == "airport_to_city"


async def test_code_with_terminal_number_picks_that_terminal(out: list[str]) -> None:
    flow, _ = flow_with(out, PlacesMaps(terminals=ORD_TERMINALS))
    await flow.start("book a ride from ORD Terminal 5 to 233 s wacker dr")
    pickup = flow.d.outbound.pickup
    assert pickup is not None
    assert (pickup.kind, pickup.airport_id) == ("airport", 2)
    assert pickup.short.startswith("ORD Terminal 5")
    assert flow.menu is None  # the terminal is known, so no terminal menu
    assert flow.d.outbound.transfer_type == "airport_to_city"


async def test_google_terminal_not_typed_airport_is_still_an_airport(out: list[str]) -> None:
    flow, _ = flow_with(out, PlacesMaps())
    await flow.start("book a ride from 233 s wacker dr to o'hare terminal 5")
    dropoff = flow.d.outbound.dropoff
    assert dropoff is not None
    assert (dropoff.kind, dropoff.airport_id) == ("airport", 2)
    assert flow.d.outbound.transfer_type == "city_to_airport"
