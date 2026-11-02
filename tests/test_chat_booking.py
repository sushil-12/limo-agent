"""Booking a ride from the web chat (app.services.booking.chat) and through POST /v1/chat."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.schemas.chat import BookedOut, BookingPreviewOut
from app.services.booking.chat import (
    INTERRUPTED_REPLY,
    ROLE_REPLY,
    SIGN_IN_REPLY,
    UNAVAILABLE_REPLY,
    BookingReply,
    BookingSessions,
    is_booking_request,
)
from app.services.booking.reference import airport_index
from app.services.prompts import build_system_prompt
from app.services.requester import Requester
from tests.conftest import DRIVER_ID, RIDER_ID, TokenFactory, auth
from tests.test_booking import NOW, FakeApi, FakeMaps
from tests.test_chat import body, ollama_reply, parse_sse

TRIP = "book 5 am pickup on 14 nov from ord to mdw"
RIDER = Requester(role="rider", display_name="Asha", user_id=RIDER_ID)
CDN = "https://api.1800limo.com/storage/images"


class PhotoApi(FakeApi):
    """FakeApi whose listings carry photos the way QuoteController returns them."""

    async def master_vehicle_listing(self, payload: dict[str, Any]) -> dict[str, Any]:
        answer = await super().master_vehicle_listing(payload)
        for category in answer["data"]:
            category["cat_img"] = f"{CDN}/cat-{category['id']}.png"
        return answer

    async def vehicle_listing(self, payload: dict[str, Any]) -> dict[str, Any]:
        answer = await super().vehicle_listing(payload)
        for vehicle in answer["data"]:
            vehicle["vehicle_images"] = [
                f"{CDN}/v-{vehicle['id']}-a.jpg",
                f"{CDN}/v-{vehicle['id']}-b.jpg",
                "javascript:alert(1)",  # never passed on
            ]
        return answer


# Answers to the flight / meet & greet / instructions questions, by pending question.
DETAIL_ANSWERS = {
    "airline_search:0:pickup": "AA",
    "detail:0:pickup_flight": "AA100",
    "detail:0:city": "Dallas",
    "meet_default:0": "OK",
    "airline_search:0:dropoff": "UA",
    "detail:0:dropoff_flight": "Skip",
    "detail:0:instructions": "Skip",
}


class Harness:
    def __init__(self, *, maps: bool = True, clock: Callable[[], float] | None = None) -> None:
        self.api = PhotoApi()
        self.tokens: list[str] = []
        self.sessions = BookingSessions(
            api_factory=self._api_for,
            maps=FakeMaps() if maps else None,  # type: ignore[arg-type]
            airports=airport_index,
            now=lambda: NOW,
            **({"clock": clock} if clock is not None else {}),
        )

    async def answer_details(self, reply: BookingReply | None) -> BookingReply | None:
        """Answer each detail question the flow asks until it asks to confirm."""
        flow = self.sessions._sessions[f"{RIDER_ID}:c1"].flow
        while flow.pending in DETAIL_ANSWERS:
            reply = await self.say(DETAIL_ANSWERS[flow.pending])
        return reply

    def _api_for(self, token: str) -> Any:
        self.tokens.append(token)
        return self.api

    async def say(
        self, text: str, *, conversation: str = "c1", who: Requester = RIDER
    ) -> BookingReply | None:
        return await self.sessions.handle(who, conversation, "rider-token", text)


async def test_only_booking_requests_start_a_booking() -> None:
    harness = Harness()
    assert await harness.say("how many bookings do I have?") is None
    assert await harness.say("what does an airport transfer cost?") is None
    assert is_booking_request("Book a ride")
    assert is_booking_request("kal subah 5 baje ORD se MDW gaadi chahiye")
    for text in (
        "help me create a booking",
        "i need to make a booking for mdw to 4217 oak avenue brookfield at 5pm",
        "i want to book another",
        "can you make a reservation for me",
        "help me book",
    ):
        assert is_booking_request(text), text
    for text in (
        "how do I make a booking?",
        "did I book a ride yesterday?",
        "show my new bookings",
        "how many bookings did I make this month",
    ):
        assert not is_booking_request(text), text
    assert harness.api.calls == []


async def test_rider_books_from_the_chat_with_photo_cards() -> None:
    harness = Harness()
    first = await harness.say(TRIP)
    assert first is not None
    assert first.active
    assert "One way, round trip, or charter" in first.content
    assert first.replies == ("One way", "Round trip", "Charter", "Cancel")
    assert not first.content.startswith(" ")  # the shell's indent is dropped

    await harness.say("One way")
    await harness.say("2")
    stops = await harness.say("1")
    assert stops is not None
    assert "Any extra stops on the way?" in stops.content
    assert stops.replies == ("No", "Cancel")
    categories = await harness.say("No")
    assert categories is not None
    assert categories.cards_kind == "category"
    names = [card["title"] for card in categories.cards]
    assert names == [
        "Mid-Size Sedan X",
        "Mid-Size SUV",
        "Full Size SUV XL",
        "Classic / Antique",
        "Water Taxi",
    ]
    assert "Book now" in categories.cards[3]["details"]  # no operator vehicles: booked as the category
    suv = categories.cards[1]
    assert suv["number"] == 2
    assert suv["images"] == [f"{CDN}/cat-3.png"]
    assert suv["price"] == "$104.76 one way"
    assert "1. Mid-Size Sedan X" not in categories.content  # the cards replace the numbered list
    assert "Vehicle categories for 2 passenger(s)" in categories.content

    again = await harness.say("hmm")  # not understood: the open menu's cards come back
    assert again is not None
    assert "didn't get that" in again.content
    assert again.cards == categories.cards

    vehicles = await harness.say("2")
    assert vehicles is not None
    assert vehicles.cards_kind == "vehicle"
    tucson = vehicles.cards[0]
    assert tucson["title"] == "2024 Hyundai Tucson"
    assert tucson["subtitle"] == "Mid-Size SUV"
    assert tucson["images"] == [f"{CDN}/v-501-a.jpg", f"{CDN}/v-501-b.jpg"]
    assert all("SKYLIMO" not in detail for detail in tucson["details"])  # operator is admin-only
    assert vehicles.replies == ("Back", "Cancel")

    airline = await harness.say("1")
    assert airline is not None
    assert airline.cards == ()
    assert "Booking on your account" in airline.content
    confirm = await harness.answer_details(airline)
    assert confirm is not None
    assert "Booking summary" not in confirm.content  # the preview card replaces the text summary
    assert confirm.content.rstrip().endswith("Confirm booking? (yes/no)")
    assert confirm.replies == ("Yes", "No", "Cancel")
    preview = confirm.preview
    assert preview is not None
    assert preview["service"] == "One way"
    (leg,) = preview["legs"]
    assert leg["title"] == "Your trip"
    assert leg["when"] == "Sat 14 Nov 2026 · 5:00 AM"
    assert [p["type"] for p in leg["points"]] == ["pickup", "dropoff"]
    arriving = {"label": "Arriving flight", "value": "AA - American Airlines, United States AA100"}
    assert arriving in leg["details"]
    assert preview["vehicle"]["title"] == "2024 Hyundai Tucson"
    assert preview["vehicle"]["image"] == f"{CDN}/v-501-a.jpg"
    assert str(preview["total"]).startswith("$")
    assert all("SKYLIMO" not in row["value"] for row in preview["rows"])  # operator is admin-only
    BookingPreviewOut.model_validate(preview)  # the shape the API sends
    assert harness.api.named("individual/create-reservation") == []  # nothing before the yes

    booked = await harness.say("Yes")
    assert booked is not None
    assert not booked.active
    assert booked.content == "Your ride is booked!"
    assert booked.preview is None
    card = booked.booked
    assert card is not None
    assert card["reservation_id"] == "12345"
    assert "order" not in str(card).lower()  # order ids are not shown
    assert card["legs"][0]["from"].startswith("O'Hare")
    assert BookedOut.model_validate(card).model_dump()["legs"][0]["from"] == card["legs"][0]["from"]
    assert len(harness.api.named("individual/create-reservation")) == 1
    assert set(harness.tokens) == {"rider-token"}
    assert await harness.say("1") is None  # the booking is over; the chat answers normally again


async def test_no_at_confirm_books_nothing_and_cancel_ends_the_booking() -> None:
    harness = Harness()
    await harness.say(TRIP + " one way 2 passengers 1 bag no stops")
    await harness.say("2")
    await harness.answer_details(await harness.say("1"))
    declined = await harness.say("No")
    assert declined is not None
    assert declined.active
    assert "Not booked" in declined.content
    cancelled = await harness.say("Cancel")
    assert cancelled is not None
    assert not cancelled.active
    assert "Nothing was booked" in cancelled.content
    assert not [c for c, _ in harness.api.calls if str(c).endswith("create-reservation")]


async def test_bookings_are_kept_per_user_and_conversation() -> None:
    harness = Harness()
    await harness.say(TRIP, conversation="c1")
    other_user = Requester(role="rider", display_name="Bo", user_id=RIDER_ID + 100)
    assert await harness.say("One way", conversation="c1", who=other_user) is None
    assert await harness.say("One way", conversation="c2") is None
    reply = await harness.say("One way", conversation="c1")
    assert reply is not None
    assert "How many passengers?" in reply.content


async def test_who_may_book() -> None:
    harness = Harness()
    guest = Requester(role="guest", display_name="G", guest_id="g_1")
    assert await harness.sessions.handle(guest, "c1", None, TRIP) == BookingReply(SIGN_IN_REPLY, active=False)
    driver = Requester(role="driver", display_name="D", user_id=DRIVER_ID)
    assert await harness.say(TRIP, who=driver) == BookingReply(ROLE_REPLY, active=False)
    assert await Harness(maps=False).say(TRIP) == BookingReply(UNAVAILABLE_REPLY, active=False)
    assert harness.api.calls == []


async def test_idle_booking_expires() -> None:
    now = [0.0]
    harness = Harness(clock=lambda: now[0])
    await harness.say(TRIP)
    now[0] += 1801
    assert await harness.say("One way") is None


@pytest.mark.parametrize(
    "previous",
    [
        "What pickup time? (e.g. 5 am, 17:30)",
        "Drop-off: ORD Terminal 2 (airport)\nWhat date? (e.g. 14 nov, tomorrow)",
        "Which terminal at O'Hare for the drop-off?\n1. Terminal 1\n2. Terminal 2",
        "---- Booking summary ----\nTotal $104.76\nConfirm booking? (yes/no)",
    ],
)
async def test_answer_to_a_lost_booking_is_not_sent_to_the_chatbot(previous: str) -> None:
    harness = Harness()  # no open session: it went idle, or the server restarted
    reply = await harness.sessions.handle(RIDER, "c1", "rider-token", "5pm", previous)
    assert reply == BookingReply(INTERRUPTED_REPLY, active=False)
    assert harness.api.calls == []


async def test_chat_after_a_finished_reply_still_chats() -> None:
    harness = Harness()
    for previous in ("Booked. Reservation #12345.", "Midway has two terminals. Anything else?"):
        assert await harness.sessions.handle(RIDER, "c1", "rider-token", "thanks", previous) is None


async def test_a_trip_typed_as_the_pickup_answer_is_read_as_a_trip() -> None:
    harness = Harness()
    await harness.say("help me create a booking")
    flow = harness.sessions._sessions[f"{RIDER_ID}:c1"].flow
    assert flow.pending == "pickup"
    reply = await harness.say("book a ride from ord to mdw for 14 nov at 5pm")
    assert reply is not None
    assert "One way, round trip, or charter" in reply.content
    out = flow.d.outbound
    assert (out.pickup.airport_id, out.dropoff.airport_id) == (2, 117)
    assert (str(out.date), out.time) == ("2026-11-14", "17:00:00")


async def test_a_turn_finishes_when_the_client_disconnects() -> None:
    harness = Harness()
    gate = asyncio.Event()
    rates = harness.api.booking_rates

    async def slow_rates(data: dict[str, Any]) -> dict[str, Any]:
        await gate.wait()
        return await rates(data)

    harness.api.booking_rates = slow_rates  # type: ignore[method-assign]
    await harness.say(TRIP + " one way 2 passengers 1 bag no stops")
    await harness.say("2")
    turn = asyncio.ensure_future(harness.say("1"))  # picks the Tucson; pricing waits on the gate
    await asyncio.sleep(0)
    turn.cancel()  # the client went away
    with pytest.raises(asyncio.CancelledError):
        await turn
    gate.set()
    await asyncio.sleep(0.01)
    flow = harness.sessions._sessions[f"{RIDER_ID}:c1"].flow
    assert flow.d.rates is not None  # the turn ran to the end
    reply = await harness.say("AA")
    assert reply is not None
    assert "Booking on your account" in reply.content  # what the lost turn said comes now


async def test_failure_inside_a_turn_ends_the_booking() -> None:
    harness = Harness()

    async def broken(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("bug")

    harness.api.master_vehicle_listing = broken  # type: ignore[method-assign]
    reply = await harness.say(TRIP + " one way 2 passengers 1 bag no stops")
    assert reply is not None
    assert not reply.active
    assert "stopped this booking" in reply.content
    assert not harness.sessions.active(RIDER, "c1")


# --- through POST /v1/chat -----------------------------------------------------------------


@pytest.fixture
def booking_client(client_factory: Callable[..., TestClient]) -> tuple[TestClient, Harness]:
    client = client_factory(chat_booking_enabled=True)
    harness = Harness()
    client.app.state.chat_service._booking = harness.sessions  # type: ignore[attr-defined]
    return client, harness


def test_chat_endpoint_streams_booking_events(
    booking_client: tuple[TestClient, Harness], make_token: TokenFactory
) -> None:
    client, harness = booking_client
    token = make_token(RIDER_ID)
    harness.sessions._api_factory = lambda t: (harness.tokens.append(t), harness.api)[1]  # type: ignore[assignment]

    response = client.post(
        "/v1/chat",
        headers=auth(token),
        json=body(("user", TRIP + " one way 2 passengers 1 bag no stops"), stream=True, conversation_id="c9"),
    )
    events = parse_sse(response.text)
    assert [name for name, _ in events] == ["start", "booking", "delta", "done"]
    booking = events[1][1]
    assert booking["active"] is True
    assert booking["cards_kind"] == "category"
    assert booking["cards"][1]["images"] == [f"{CDN}/cat-3.png"]
    assert events[3][1]["reply_type"] == "booking"
    assert harness.tokens == [token]  # the user's own token reaches the 1800limo API

    picked = client.post(
        "/v1/chat",
        headers=auth(token),
        json=body(("user", "book a ride"), ("assistant", "..."), ("user", "2"), conversation_id="c9"),
    ).json()
    assert picked["reply_type"] == "booking"
    assert picked["booking"]["cards_kind"] == "vehicle"
    assert picked["booking"]["cards"][0]["images"][0] == f"{CDN}/v-501-a.jpg"


def test_chat_endpoint_without_booking_words_still_chats(
    booking_client: tuple[TestClient, Harness], make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    client, harness = booking_client
    ollama.post("/api/chat").mock(return_value=httpx.Response(200, json=ollama_reply()))
    data = client.post("/v1/chat", headers=auth(make_token(RIDER_ID)), json=body(("user", "Hi"))).json()
    assert data["reply_type"] == "answer"
    assert data["booking"] is None
    assert harness.api.calls == []


@pytest.mark.parametrize("can_look_up", [True, False])
def test_prompt_explains_chat_booking_only_to_users_who_can_book(can_look_up: bool) -> None:
    rider = build_system_prompt(RIDER, can_look_up=can_look_up, can_book=True)
    assert "book a ride from ORD" in rider
    assert "cannot create" not in rider
    assert "create, change or cancel" not in rider
    assert "edit booking 2141" in rider
    assert "You cannot cancel bookings" in rider
    other = build_system_prompt(RIDER, can_look_up=can_look_up)
    assert "book a ride from ORD" not in other
    assert "create, change or cancel bookings" in other


def test_sessions_offer_booking_by_role() -> None:
    harness = Harness()
    assert harness.sessions.offers(RIDER)
    assert not harness.sessions.offers(Requester(role="driver", display_name=None, user_id=DRIVER_ID))
    assert not harness.sessions.offers(Requester(role="guest", display_name=None, guest_id="g"))
    assert not Harness(maps=False).sessions.offers(RIDER)


async def test_numbered_menus_become_buttons() -> None:
    harness = Harness()
    reply = await harness.say("book a ride from chicago airport")
    assert reply is not None
    assert [o["label"] for o in reply.options] == [
        "ORD - Chicago Ohare Intl",
        "MDW - Chicago Midway Intl",
    ]
    assert "1. ORD" not in reply.content  # the buttons replace the numbered lines
    again = await harness.say("9")  # out of range: the same buttons come back with the hint
    assert again is not None
    assert again.options == reply.options
    picked = await harness.say("2")
    assert picked is not None
    assert picked.options == ()
