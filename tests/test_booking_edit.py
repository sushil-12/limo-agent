"""Changing a saved booking (EDIT_BOOKING_FLOW.md) in the shell flow and the web chat, against
in-memory fakes of the 1800limo API, Google Maps and the bookings lookup."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pytest

from app.services.booking.api import ApiError
from app.services.booking.chat import EDIT_SIGN_IN_REPLY, BookingSessions
from app.services.booking.edit import EditFlow, booking_number, is_edit_request
from app.services.booking.lookup import BookingRow, RelatedLeg
from app.services.booking.reference import airport_index
from app.services.requester import Requester
from tests.conftest import RIDER_ID
from tests.test_booking import NOW, RATE_ARRAY, USER_ID, FakeApi, FakeMaps

ORD = airport_index().by_code("ORD")
assert ORD is not None
RESERVATION: dict[str, Any] = {
    "id": 2141,
    "reservation_id": 2141,
    "booking_status": "pending",
    "service_type": "oneway",
    "transfer_type": "city_to_airport",
    "return_transfer_type": None,
    "pickup": "233 S Wacker Dr, Chicago",
    "pickup_address": "233 S Wacker Dr, Chicago",
    "pickup_latitude": "41.8789",
    "pickup_longitude": "-87.6359",
    "dropoff": "",
    "dropoff_address": "ORD - Chicago Ohare Intl, Chicago, United States",
    "dropoff_airport": ORD.id,
    "dropoff_airport_latitude": ORD.lat,
    "dropoff_airport_longitude": ORD.long,
    "dropoff_airline": 1,
    "dropoff_flight": "AA100",
    "pickup_date": "2026-12-12",
    "pickup_time": "17:00:00",
    "total_passengers": 2,
    "luggage_count": 1,
    "number_of_hours": 0,
    "distance": 30000,
    "duration": 1800,
    "meet_greet_choices": 1,
    "booking_instructions": "<ol><li>Driver - Call the passenger on arrival.</li></ol>",
    "vehicle_id": 501,
    "vehicle_type": 3,
    "affiliate_id": 31,
    "affiliate_type": "affiliate",
    "driver_id": 77,
    "driver_name": "Joseph Anselmo",
    "account_type": "individual",
    "acc_id": 900,
    "travel_client_id": None,
    "passenger_name": "Asha Rider",
    "passenger_cell": "312-555-0111",
    "passenger_cell_isd": "1",
    "passenger_cell_country": "us",
    "passenger_email": "asha@example.com",
    "currency": "usd",
    "cancellation_hours": 48,
    "share_array": '{"adminShare": 20}',
    "related_to": None,
    "extra_stops": "[]",
    "is_pending_affiliate": 0,
}
AGENTS = [{"id": 700, "name": "Spears Travel (+1 9184060000)"}]
AGENT_CLIENTS = [
    {"id": 710, "name": "Lisa Taylor (+1 9184069395)"},
    {"id": 711, "name": "Ken Ito (+1 9184060001)"},
]
ACCOUNTS = {
    "902": {"first_name": "Bob", "last_name": "Stone", "email": "bob@example.com", "mobile": "3125550113"},
    "710": {"first_name": "Lisa", "last_name": "Taylor", "email": "lt@spears.com", "mobile": "9184069395"},
}
SAVED_RATES = {"rateArray": copy.deepcopy(RATE_ARRAY), "sub_total": 96.0, "grand_total": 100.0}


@dataclass
class EditApi(FakeApi):
    """FakeApi that also serves a saved booking and takes edits."""

    reservation: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(RESERVATION))
    edit_answers: list[dict[str, Any]] = field(default_factory=list)

    async def get(self, path: str) -> dict[str, Any]:
        if "get-reservation/" in path:
            self.calls.append(("GET", path))
            if f"/{self.reservation['id']}/" not in path:
                raise ApiError("This Booking Might not belongs to You", status=422)
            return {"success": True, "data": copy.deepcopy(self.reservation)}
        if path in ("get-an-account/902", "get-an-account/710"):
            self.calls.append(("GET", path))
            return {"success": True, "data": ACCOUNTS[path.rsplit("/", 1)[1]]}
        if path == "get-account-by-type/travel":
            self.calls.append(("GET", path))
            return {"success": True, "data": AGENTS}
        if path.startswith("get-account-for-travel-agent/"):
            self.calls.append(("GET", path))
            return {"success": True, "data": AGENT_CLIENTS}
        return await super().get(path)

    async def put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("PUT " + path, copy.deepcopy(body)))
        return self._edited()

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path.endswith("edit-reservation"):
            self.calls.append(("POST " + path, copy.deepcopy(body)))
            return self._edited()
        return await super().post(path, body)

    def _edited(self) -> dict[str, Any]:
        if self.edit_answers:
            return self.edit_answers.pop(0)
        return {"success": True, "message": "Reservation detail has been updated successfully.", "data": {}}

    async def reservation_rates(self, reservation_id: int) -> dict[str, Any]:
        self.calls.append(("saved_rates", reservation_id))
        return copy.deepcopy(SAVED_RATES)

    async def common_data(self) -> dict[str, Any]:
        data = await super().common_data()
        return {**data, "vehicleCategories": [{"id": 3, "name": "Mid-Size SUV"}]}

    def edits(self) -> list[dict[str, Any]]:
        return [body for call, body in self.calls if call.endswith("edit-reservation")]


@dataclass
class FakeLookup:
    rows: list[BookingRow] = field(default_factory=list)
    legs: list[RelatedLeg] = field(default_factory=list)

    async def upcoming(self, mode: str, user_id: int, today: date, limit: int = 10) -> list[BookingRow]:
        return self.rows

    async def related(self, reservation_id: int, related_to: int | None) -> list[RelatedLeg]:
        return self.legs


@pytest.fixture
def out() -> list[str]:
    return []


def make_edit(
    mode: str, out: list[str], api: EditApi | None = None, lookup: Any = None
) -> tuple[EditFlow, EditApi]:
    api = api or EditApi()
    flow = EditFlow(
        mode=mode,
        user_id=USER_ID,
        api=api,  # type: ignore[arg-type]
        maps=FakeMaps(),  # type: ignore[arg-type]
        airports=airport_index(),
        say=out.append,
        now=lambda: NOW,
        lookup=lookup,
    )
    return flow, api


def said(out: list[str]) -> str:
    return "\n".join(out)


# --- recognising the request ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Edit a Booking 2141", True),
        ("change my booking", True),
        ("can you reschedule my ride #2141", True),
        ("booking 2141 ka time badalna hai", True),
        ("how do I change my booking?", False),
        ("book a ride from ord to mdw", False),
        ("how many bookings were updated today", False),
        ("change passengers to 3", False),
    ],
)
def test_is_edit_request(text: str, expected: bool) -> None:
    assert is_edit_request(text) is expected


def test_booking_number() -> None:
    assert booking_number("Edit a Booking 2141") == 2141
    assert booking_number("edit #2141 please") == 2141
    assert booking_number("change booking time to 5:30 pm") is None


# --- admin -----------------------------------------------------------------------------------


async def test_admin_time_change_keeps_the_saved_price(out: list[str]) -> None:
    flow, api = make_edit("admin", out)
    await flow.start("Edit a Booking 2141")
    assert ("GET", "get-reservation/2141/edit") in api.calls
    assert ("saved_rates", 2141) in api.calls
    assert "Booking #2141 (awaiting confirmation)" in said(out)
    assert "$100.00 (saved)" in said(out)
    assert "What would you like to change?" in said(out)

    out.clear()
    await flow.handle("change time to 6 pm")
    assert flow.pending == "confirm"
    assert "Pickup: Sat 12 Dec 2026 5:00 PM → Sat 12 Dec 2026 6:00 PM" in said(out)
    assert "$100.00 (kept; say 'reprice' for the system price)" in said(out)
    assert api.named("rates") == []  # "Prevent System Rate Override": no reprice for a time change
    assert "Save these changes to booking #2141?" in said(out)

    await flow.handle("yes")
    (sent,) = api.edits()
    assert ("PUT edit-reservation", sent) in api.calls
    assert sent["reservation_id"] == 2141
    assert sent["updateType"] == "edit"
    assert sent["proceed"] is True
    assert sent["service_type"] == "one_way"
    assert sent["pickup_time"] == "18:00:00"
    assert sent["pickup_date"] == "2026-12-12"
    assert sent["rateArray"] == RATE_ARRAY
    assert sent["grand_total"] == 100.0
    assert sent["shares_array"] == {"adminShare": 20}
    assert sent["prevent_rate_override"] is True
    # untouched fields go back exactly as they were loaded
    assert sent["acc_id"] == 900
    assert sent["affiliate_id"] == 31
    assert sent["vehicle_id"] == 501
    assert sent["dropoff_airport"] == ORD.id
    assert sent["dropoff_flight"] == "AA100"
    assert sent["cancellation_hours"] == "48"
    assert sent["booking_instructions"] == RESERVATION["booking_instructions"]
    assert flow.finished
    assert "Booking #2141 updated." in said(out)


async def test_admin_reprice_uses_the_system_price(out: list[str]) -> None:
    flow, api = make_edit("admin", out)
    await flow.start("edit booking 2141")
    await flow.handle("change passengers to 3")
    await flow.handle("reprice")
    assert len(api.named("rates")) == 1
    assert flow.pending == "confirm"
    await flow.handle("yes")
    (sent,) = api.edits()
    assert sent["total_passengers"] == 3
    assert sent["prevent_rate_override"] is False
    assert sent["grand_total"] == flow.d.rates.grand_total  # type: ignore[union-attr]


async def test_admin_picks_another_vehicle(out: list[str]) -> None:
    flow, api = make_edit("admin", out)
    await flow.start("edit booking 2141")
    await flow.handle("vehicles")
    assert flow.menu is not None
    assert flow.menu.kind == "category"
    await flow.handle("2")  # Mid-Size SUV
    await flow.handle("2")  # the second SUV, another operator
    assert flow.pending == "confirm"
    assert "the new operator is asked to accept the ride" in said(out)
    await flow.handle("yes")
    (sent,) = api.edits()
    assert sent["vehicle_id"] == 502
    assert sent["affiliate_id"] == 32
    assert sent["affiliate_type"] == "affiliate"
    assert sent["prevent_rate_override"] is False


async def test_confirm_dialog_resends_with_proceed_false(out: list[str]) -> None:
    api = EditApi()
    api.edit_answers.append({
        "success": True, "message": "Changing the affiliate sends a new Accept/Reject email.",
        "data": {"is_confirm": False},
    })  # fmt: skip
    flow, _ = make_edit("admin", out, api)
    await flow.start("edit booking 2141")
    await flow.handle("change time to 6 pm")
    await flow.handle("yes")
    assert flow.pending == "proceed"
    assert "Save anyway?" in said(out)
    await flow.handle("yes")
    first, second = api.edits()
    assert first["proceed"] is True
    assert second["proceed"] is False
    assert flow.finished


# --- rider -----------------------------------------------------------------------------------


async def test_rider_change_is_repriced_and_posted(out: list[str]) -> None:
    flow, api = make_edit("individual", out)
    await flow.start("edit booking 2141")
    assert ("GET", "individual/get-reservation/2141/edit") in api.calls
    assert ("saved_rates", 2141) not in api.calls  # an admin-only endpoint
    await flow.handle("change passengers to 3")
    (rates_call,) = api.named("rates")
    assert rates_call["vehicle_id"] == 501
    assert rates_call["affiliate_id"] == 31
    assert rates_call["distance"] == 30000
    assert "Passengers: 2 → 3" in said(out)
    await flow.handle("yes")
    (sent,) = api.edits()
    assert ("POST individual/edit-reservation", sent) in api.calls
    assert sent["prevent_rate_override"] is False
    assert sent["grand_total"] == flow.d.rates.grand_total  # type: ignore[union-attr]
    assert "booking_created_from" not in sent


async def test_rider_cannot_move_a_booking_into_the_past(out: list[str]) -> None:
    flow, api = make_edit("individual", out)
    await flow.start("edit booking 2141")
    await flow.handle("change date to 1 oct 2026")
    assert "in the past" in said(out)
    assert api.edits() == []


async def test_no_number_lists_upcoming_bookings(out: list[str]) -> None:
    lookup = FakeLookup(rows=[
        BookingRow(2140, datetime(2026, 11, 2, 9, 0), "Hilton Chicago", "MDW"),
        BookingRow(2141, datetime(2026, 12, 12, 17, 0), "233 S Wacker Dr", "ORD"),
    ])  # fmt: skip
    flow, _ = make_edit("individual", out, lookup=lookup)
    await flow.start("change my booking")
    assert flow.menu is not None
    assert flow.menu.kind == "edit_pick"
    assert "2. #2141 — Sat 12 Dec 2026 5:00 PM — 233 S Wacker Dr → ORD" in said(out)
    await flow.handle("2")
    assert flow.reservation_id == 2141


async def test_unknown_booking_is_not_found(out: list[str]) -> None:
    flow, _ = make_edit("individual", out)
    await flow.start("edit booking 9999")
    assert "couldn't find booking #9999 on your account" in said(out)
    assert flow.pending == "edit_id"
    assert not flow.finished
    await flow.handle("2141")
    assert flow.reservation_id == 2141


@pytest.mark.parametrize("status", ["paid", "paid_cash", "cancelled", "ended", "finalized", "refund"])
async def test_locked_bookings_are_refused(out: list[str], status: str) -> None:
    api = EditApi()
    api.reservation["booking_status"] = status
    flow, _ = make_edit("individual", out, api)
    await flow.start("edit booking 2141")
    assert flow.finished
    assert "can't be changed" in said(out)
    assert api.edits() == []


async def test_round_trip_is_refused_and_riders_keep_their_account(out: list[str]) -> None:
    flow, api = make_edit("individual", out)
    await flow.start("edit booking 2141")
    await flow.handle("change service to round trip")
    assert "book it as a new ride" in said(out)
    await flow.handle("change customer to Bob")
    assert "A booking stays on your own account" in said(out)
    assert flow.menu is None
    assert flow.d.service_type == "one_way"
    assert api.named("rates") == []
    assert api.edits() == []


async def test_undo_and_cancel(out: list[str]) -> None:
    flow, api = make_edit("individual", out)
    await flow.start("edit booking 2141")
    await flow.handle("change passengers to 3")
    await flow.handle("undo")
    assert flow.d.pax == 2
    assert "Nothing has changed yet." in said(out)
    await flow.handle("cancel")
    assert flow.finished
    assert "Booking #2141 was not changed." in said(out)
    assert api.edits() == []


async def test_moving_past_the_return_leg_is_warned(out: list[str]) -> None:
    lookup = FakeLookup(legs=[RelatedLeg(2142, datetime(2026, 12, 13, 9, 0), is_outbound=False)])
    flow, _ = make_edit("admin", out, lookup=lookup)
    await flow.start("edit booking 2141")
    await flow.handle("change date to 14 dec")
    assert "the return ride, booking #2142, is on Sun 13 Dec 2026 9:00 AM, before the new pickup" in said(out)


async def test_notes_are_added_to_the_saved_instructions(out: list[str]) -> None:
    flow, api = make_edit("admin", out)
    await flow.start("edit booking 2141")
    await flow.handle("change notes to gate code 1234")
    await flow.handle("yes")
    (sent,) = api.edits()
    assert sent["booking_instructions"] == RESERVATION["booking_instructions"] + "<p>gate code 1234</p>"


# --- web chat ----------------------------------------------------------------------------------


RIDER = Requester(role="rider", display_name="Asha", user_id=RIDER_ID)


def chat_sessions(api: EditApi) -> BookingSessions:
    return BookingSessions(
        api_factory=lambda token: api,  # type: ignore[arg-type,return-value]
        maps=FakeMaps(),  # type: ignore[arg-type]
        airports=airport_index,
        lookup=FakeLookup(),  # type: ignore[arg-type]
        now=lambda: NOW,
    )


async def test_chat_edits_a_booking() -> None:
    api = EditApi()
    sessions = chat_sessions(api)
    first = await sessions.handle(RIDER, "c1", "rider-token", "Edit a Booking 2141")
    assert first is not None
    assert first.active
    assert "Booking #2141" in first.content
    assert "Undo" in first.replies
    second = await sessions.handle(RIDER, "c1", "rider-token", "change passengers to 3")
    assert second is not None
    assert "Passengers: 2 → 3" in second.content
    assert second.replies[:2] == ("Yes", "No")
    done = await sessions.handle(RIDER, "c1", "rider-token", "yes")
    assert done is not None
    assert "Booking #2141 updated." in done.content
    assert not done.active
    assert len(api.edits()) == 1


async def test_chat_edit_needs_sign_in() -> None:
    sessions = chat_sessions(EditApi())
    guest = Requester(role="guest", display_name=None, guest_id="g")
    reply = await sessions.handle(guest, "c1", None, "edit booking 2141")
    assert reply is not None
    assert reply.content == EDIT_SIGN_IN_REPLY


async def test_several_changes_in_one_message(out: list[str]) -> None:
    flow, _ = make_edit("admin", out)
    await flow.start("edit booking 2141")
    await flow.handle("change pax to 4 and bags to 2")
    assert "Passengers: 2 → 4" in said(out)
    assert "Bags: 1 → 2" in said(out)


# --- changing the customer -------------------------------------------------------------------


async def test_admin_moves_a_booking_to_another_customer(out: list[str]) -> None:
    flow, api = make_edit("admin", out)
    await flow.start("edit booking 2141")
    await flow.handle("change the customer")
    assert flow.menu is not None
    assert flow.menu.kind == "edit_customer"
    assert flow.menu.options == ["individual", "travel_planner"]  # the shell offers no new customers
    await flow.handle("1")
    await flow.handle("Bob")
    await flow.handle("1")  # Bob Stone, account 902
    assert flow.pending == "passenger_choice"
    await flow.handle("2")  # keep the passenger
    assert "Customer: Asha Rider → Bob Stone (+1 3125550113)" in said(out)
    await flow.handle("yes")
    (sent,) = api.edits()
    assert sent["account_type"] == "individual"
    assert sent["acc_id"] == 902
    assert sent["passenger_name"] == "Asha Rider"
    assert sent["grand_total"] == 100.0  # the saved price stays


async def test_admin_moves_a_booking_to_a_travel_agents_client_and_the_split_follows(out: list[str]) -> None:
    api = EditApi()
    api.reservation["created_by"] = 1340  # booked by the customer, not the admin
    flow, _ = make_edit("admin", out, api)
    await flow.start("edit booking 2141")
    await flow.handle("change customer")
    await flow.handle("2")  # a travel agent's client
    await flow.handle("spears")
    await flow.handle("1")  # the agent
    await flow.handle("1")  # Lisa Taylor
    await flow.handle("1")  # she is the passenger too
    await flow.handle("yes")
    (sent,) = api.edits()
    assert sent["account_type"] == "travel_planner"
    assert sent["acc_id"] == 700
    assert sent["travel_client_id"] == 710
    assert sent["travel_client_acc"] == "travel_individual"
    assert sent["grand_total"] == 100.0
    assert "travelAgentShare" in sent["shares_array"]  # recalculated for the agent's client
    assert sent["shares_array"] != {"adminShare": 20}


async def test_new_customer_card_goes_through_the_chat_widget() -> None:
    api = EditApi()
    sessions = chat_sessions(api)
    admin = Requester(role="admin", display_name="Ops", user_id=1)

    async def say(text: str) -> Any:
        return await sessions.handle(admin, "c9", "admin-token", text)

    await say("edit booking 2141")
    menu = await say("change customer")
    assert "3. A new customer" in menu.content
    for answer in ("3", "Lisa", "Taylor", "ltaylor@spearstravel.com", "+19184069395", "1"):
        reply = await say(answer)
    assert "Customer: Asha Rider → Lisa Taylor (new customer)" in reply.content
    asked = await say("yes")
    request = asked.card_request
    assert request is not None
    assert request["method"] == "PUT"
    assert request["path"] == "edit-reservation"
    assert request["payload"]["account_type"] == "loose_customer"
    assert request["payload"]["loose_customer"]["first_name"] == "Lisa"
    assert request["payload"]["loose_customer"]["card_details"]["card_number"] == ""
    assert api.edits() == []  # the widget sends it, with the card

    refused = await say("card:error Your card number is incorrect.")
    assert "was not changed: Your card number is incorrect." in refused.content
    assert refused.card_request is not None  # the form is shown again

    api.reservation["acc_id"] = 950  # what the widget's request did to the booking
    done = await say("card:ok")
    assert "Booking #2141 updated, with the new customer and their card." in done.content
    assert not done.active


async def test_card_ok_is_checked_against_the_booking() -> None:
    api = EditApi()
    sessions = chat_sessions(api)
    admin = Requester(role="admin", display_name="Ops", user_id=1)
    for text in (
        "edit booking 2141",
        "change customer",
        "3",
        "Lisa",
        "Taylor",
        "l@x.com",
        "+19184069395",
        "1",
        "yes",
    ):
        await sessions.handle(admin, "c9", "t", text)
    reply = await sessions.handle(admin, "c9", "t", "card:ok")  # typed, nothing was sent
    assert reply is not None
    assert "couldn't confirm" in reply.content


async def test_follow_up_after_an_edit_reaches_the_edit_flow() -> None:
    api = EditApi()
    sessions = chat_sessions(api)
    admin = Requester(role="admin", display_name="Ops", user_id=1)
    await sessions.handle(admin, "c2", "t", "edit booking 2141")
    await sessions.handle(admin, "c2", "t", "change pax to 4")
    saved = await sessions.handle(admin, "c2", "t", "yes")
    assert saved is not None
    assert not saved.active
    follow = await sessions.handle(admin, "c2", "t", "i need to update the cliet account")
    assert follow is not None
    assert "Booking #2141" in follow.content
    assert "Who should booking #2141 be for?" in follow.content
    assert await sessions.handle(admin, "c3", "t", "update the client account") is None  # other chat


def test_the_model_never_sees_booking_flow_output() -> None:
    from app.schemas.chat import ChatMessageIn
    from app.services.chat_service import _llm_history

    turns = _llm_history([
        ChatMessageIn(role="user", content="edit booking 2141"),
        ChatMessageIn(role="assistant", content="---- Changes to booking #2141 ----\nPassengers: 2 → 4"),
        ChatMessageIn(role="user", content="card:ok"),
        ChatMessageIn(role="assistant", content="Booking #2141 updated."),
        ChatMessageIn(role="user", content="thanks"),
    ])  # fmt: skip
    text = "\n".join(t.content for t in turns)
    assert "Changes to booking" not in text
    assert "updated" not in text
    assert "card:ok" not in text
    assert turns[-1].content == "thanks"
