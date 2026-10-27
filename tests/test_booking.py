"""The shell's "book a ride" flow (BOOKING_FLOW.md), against in-memory fakes of the 1800limo
API and Google Maps. The 11 scenarios from the task brief come first, then the pieces."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import respx

from app.services.booking.api import ApiError, ApiUnreachableError, LimoApi
from app.services.booking.card_form import collect_card, validate_card
from app.services.booking.draft import BookingDraft, Leg, Place
from app.services.booking.flow import BookingFlow
from app.services.booking.maps import PlaceDetails, Route, Suggestion
from app.services.booking.parse import parse_trip
from app.services.booking.payload import form_value, quote_payload
from app.services.booking.rates import (
    RatesError,
    ShareFlags,
    outbound_rates,
    return_rates,
    share_array,
    to_fixed2,
)
from app.services.booking.reference import airport_index, currency_for

TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=TZ)
USER_ID = 4242

CATEGORIES = [
    {
        "id": 1,
        "name": "Mid-Size Sedan X",
        "passenger": 4,
        "luggage": 2,
        "vehicle_count": 3,
        "rate_breakdown_one_way": {"grand_total": 87.98},
        "rate_breakdown_round_trip": {"grand_total": 175.96},
    },
    {
        "id": 3,
        "name": "Mid-Size SUV",
        "passenger": 4,
        "luggage": 4,
        "vehicle_count": 2,
        "rate_breakdown_one_way": {"grand_total": 104.76},
        "rate_breakdown_round_trip": {"grand_total": 209.52},
    },
    {
        "id": 5,
        "name": "Full Size SUV XL",
        "passenger": 7,
        "luggage": 7,
        "vehicle_count": 4,
        "rate_breakdown_one_way": {"grand_total": 150.10},
        "rate_breakdown_round_trip": {"grand_total": 300.20},
    },
    {
        "id": 9,
        "name": "Classic / Antique",
        "passenger": 4,
        "luggage": 2,
        "vehicle_count": 0,
        "rate_breakdown_one_way": {"grand_total": 500},
        "rate_breakdown_round_trip": {"grand_total": 1000},
    },
    {
        "id": 11,
        "name": "Water Taxi",
        "passenger": 10,
        "luggage": 10,
        "vehicle_count": 0,
        "rate_breakdown_one_way": {"grand_total": 300},
        "rate_breakdown_round_trip": {"grand_total": 600},
    },
]
TYPE_IDS = {"Mid-Size Sedan X": 1, "Mid-Size SUV": 3, "Full Size SUV XL": 5, "Classic / Antique": 9}
DRIVER = {
    "id": 77,
    "name": "Joseph Anselmo",
    "gender": "male",
    "email": "joe@example.com",
    "cell_number": "3125550100",
    "cell_isd": "1",
    "cell_country": "us",
}
SUVS = [
    {
        "id": 501,
        "affiliate_id": 31,
        "affiliate_name": "Joseph Anselmo",
        "affiliate_company": "SKYLIMO",
        "affiliate_phone": "+1 312 555 0199",
        "vehicle_type_id": 3,
        "name": "Mid-Size SUV",
        "passenger": 4,
        "luggage": 4,
        "cancellation_policy": 48,
        "is_master_vehicle": False,
        "driverInformation": DRIVER,
        "vehicle_details": {
            "year": 2024,
            "make": "Hyundai",
            "model": "Tucson",
            "make_id": 8,
            "model_id": 81,
            "year_id": 24,
            "seats": 4,
        },
        "rate_breakdown_one_way": {"grand_total": 104.76},
        "rate_breakdown_round_trip": {"grand_total": 209.52},
    },
    {
        "id": 502,
        "affiliate_id": 32,
        "affiliate_name": "Other Op",
        "affiliate_company": "OTHER LIMO",
        "affiliate_phone": "+1 312 555 0123",
        "vehicle_type_id": 3,
        "name": "Mid-Size SUV",
        "passenger": 4,
        "luggage": 4,
        "cancellation_policy": 24,
        "is_master_vehicle": False,
        "driverInformation": {},
        "vehicle_details": {"year": 2023, "make": "Toyota", "model": "RAV4"},
        "rate_breakdown_one_way": {"grand_total": 124.36},
        "rate_breakdown_round_trip": {"grand_total": 248.72},
    },
]
RATE_ARRAY = {
    "all_inclusive_rates": {
        "Base_Rate": {"rate_label": "Base Rate", "baserate": 80, "amount": 80, "type": "flat"},
        "ELH_Charges": {"rate_label": "ELH", "baserate": 0, "amount": 0, "type": "flat"},
        "Stops": {"rate_label": "Stops", "baserate": 0, "amount": 0, "type": "flat"},
        "Wait": {"rate_label": "Wait", "baserate": 0, "amount": 0, "type": "flat"},
    },
    "direct_taxes": {},
    "taxes": {"Trip_Tax": {"rate_label": "Trip Tax", "baserate": 10, "amount": 0, "type": "percent"}},
    "amenities": {},
    "misc": {"Extra_Gratuity": {"rate_label": "Gratuity", "baserate": 16, "amount": 16, "type": "flat"}},
}
AIRLINES = [
    {"id": 1, "code": "AA", "name": "American Airlines", "country": "United States"},
    {"id": 2, "code": "UA", "name": "United Airlines", "country": "United States"},
]
MEET_GREETS = [
    {"id": 1, "message": "Driver - Text/call when on location"},
    {"id": 2, "message": "Driver -  Airport - Text/call after plane lands with curbside meet location"},
]
INDIVIDUALS = [
    {"id": 900, "name": "Asha Rider (+1 3125550111)"},
    {"id": 901, "name": "Asha Patel (+1 3125550112)"},
    {"id": 902, "name": "Bob Stone (+1 3125550113)"},
]
ACCOUNT = {
    "first_name": "Asha",
    "middle_name": None,
    "last_name": "Rider",
    "email": "asha@example.com",
    "mobile": "3125550111",
    "mobileIsd": "+1",
    "mobileCountry": "us",
}


@dataclass
class FakeApi:
    """Records every call; answers like the Laravel API."""

    calls: list[tuple[str, Any]] = field(default_factory=list)
    vehicles: list[dict[str, Any]] = field(default_factory=lambda: copy.deepcopy(SUVS))
    create_error: ApiError | None = None

    async def get(self, path: str) -> dict[str, Any]:
        self.calls.append(("GET", path))
        if path.endswith("get-account-by-type/individual"):
            return {"success": True, "data": INDIVIDUALS}
        if "get-an-account/" in path or path == "individual/get-account-details":
            return {"success": True, "data": ACCOUNT}
        raise ApiError("Not found", status=404)

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((path, copy.deepcopy(body)))
        if path.endswith("create-reservation"):
            if self.create_error is not None:
                raise self.create_error
            return {
                "success": True,
                "message": "Reservation has been created successfully.",
                "data": {"reservation_id": 12345, "order_id": "ORD-1"},
            }
        raise ApiError("Not found", status=404)

    async def master_vehicle_listing(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("master", copy.deepcopy(payload)))
        return {"success": True, "data": copy.deepcopy(CATEGORIES), "currency": {"symbol": "$"}}

    async def vehicle_listing(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("vehicles", copy.deepcopy(payload)))
        return {"success": True, "data": copy.deepcopy(self.vehicles), "currency": {"symbol": "$"}}

    async def vehicle_type_ids(self) -> dict[str, int]:
        self.calls.append(("filters", None))
        return dict(TYPE_IDS)

    async def booking_rates(self, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("rates", copy.deepcopy(data)))
        return {"rateArray": copy.deepcopy(RATE_ARRAY), "min_rate_involved": False}

    async def master_vehicle_info(self, vehicle_id: int) -> dict[str, Any]:
        return {}

    async def common_data(self) -> dict[str, Any]:
        return {"airlinesData": AIRLINES, "meetGreets": MEET_GREETS, "vehicleCategories": []}

    def named(self, name: str) -> list[Any]:
        return [body for call, body in self.calls if call == name]


def _place(place_id: str, name: str, address: str, lat: float, lng: float, *, airport: bool) -> PlaceDetails:
    return PlaceDetails(
        place_id=place_id,
        name=name,
        formatted_address=address,
        lat=lat,
        lng=lng,
        types=("airport",) if airport else ("establishment",),
        country_names=("Chicago", "United States"),
    )


PLACES = {
    "ord": _place(
        "p-ord",
        "O'Hare International Airport",
        "10000 W Balmoral Ave, Chicago, IL 60666, USA",
        41.9803,
        -87.9090,
        airport=True,
    ),
    "mdw": _place(
        "p-mdw",
        "Chicago Midway International Airport",
        "5700 S Cicero Ave, Chicago, IL 60638, USA",
        41.7868,
        -87.7522,
        airport=True,
    ),
    "wacker": _place(
        "p-wacker",
        "233 S Wacker Dr",
        "233 S Wacker Dr, Chicago, IL 60606, USA",
        41.8789,
        -87.6359,
        airport=False,
    ),
}


@dataclass
class FakeMaps:
    route_calls: list[list[tuple[float, float]]] = field(default_factory=list)

    async def autocomplete(self, text: str) -> list[Suggestion]:
        lowered = text.lower()
        if lowered.startswith("ord "):
            return [Suggestion("p-ord", "O'Hare International Airport", ("airport",))]
        if lowered.startswith("mdw "):
            return [Suggestion("p-mdw", "Chicago Midway International Airport", ("airport",))]
        if "wacker" in lowered:
            return [Suggestion("p-wacker", "233 S Wacker Dr, Chicago", ("street_address",))]
        return []

    async def details(self, place_id: str) -> PlaceDetails:
        return next(p for p in PLACES.values() if p.place_id == place_id)

    async def route(self, points: list[tuple[float, float]]) -> Route:
        self.route_calls.append(points)
        return Route(meters=45299, seconds=2290)


@pytest.fixture
def out() -> list[str]:
    return []


def make_flow(mode: str, out: list[str], api: FakeApi | None = None) -> tuple[BookingFlow, FakeApi]:
    api = api or FakeApi()
    flow = BookingFlow(
        mode=mode,
        user_id=USER_ID,
        api=api,  # type: ignore[arg-type]
        maps=FakeMaps(),  # type: ignore[arg-type]
        airports=airport_index(),
        say=out.append,
        now=lambda: NOW,
    )
    return flow, api


async def run(flow: BookingFlow, *lines: str) -> list[str | None]:
    """Feed lines; return the question asked after each (pending key, or the menu kind)."""
    asked = []
    for line in lines:
        await flow.handle(line)
        asked.append(flow.pending or (flow.menu.kind if flow.menu else None))
    return asked


async def to_vehicle_menu(mode: str, out: list[str], api: FakeApi | None = None, *, trip: str = "") -> tuple:
    flow, api = make_flow(mode, out, api)
    await flow.start(trip or "5 am pickup on 14 nov from ord to mdw")
    await run(flow, "1", "2", "1", "2")  # one way, 2 pax, 1 bag, category 2 = Mid-Size SUV
    return flow, api


# --- the 11 scenarios from the brief ---------------------------------------------------


async def test_1_parses_trip_and_asks_only_service_pax_luggage(out: list[str]) -> None:
    flow, api = make_flow("admin", out)
    await flow.start("5 am pickup on 14 nov from ord to mdw")
    asked = [flow.pending, *await run(flow, "one way", "2", "1")]

    assert asked == ["service", "pax", "luggage", "category"]
    leg = flow.d.outbound
    assert (leg.date, leg.time) == (date(2026, 11, 14), "05:00:00")
    assert (leg.pickup.kind, leg.pickup.airport_id) == ("airport", 2)  # ORD's internal id
    assert (leg.dropoff.kind, leg.dropoff.airport_id) == ("airport", 117)  # MDW
    quote = api.named("master")[0]
    assert quote["pickup_type"] == quote["dropoff_type"] == "airport"
    assert (quote["pickup_date"], quote["pickup_time"]) == ("2026-11-14", "05:00:00")
    assert (quote["pickup_airport"], quote["dropoff_airport"]) == (2, 117)
    assert quote["location_info"] == [
        {"distance": {"text": "45.3 km", "value": 45299}, "duration": {"text": "38 mins", "value": 2290}}
    ]


async def test_2_chicago_airport_asks_which_one(out: list[str]) -> None:
    flow, _ = make_flow("admin", out)
    await flow.start("pickup from Chicago airport")

    assert flow.menu is not None
    assert flow.menu.kind == "place:pickup"
    assert [option[1].code for option in flow.menu.options] == ["ORD", "MDW"]
    assert flow.d.outbound.pickup is None
    await flow.handle("2")
    assert flow.d.outbound.pickup.airport_id == 117


async def test_3_big_group_hides_small_and_empty_categories(out: list[str]) -> None:
    flow, _ = make_flow("admin", out)
    await flow.start("5 am pickup on 14 nov from ord to mdw, 6 passengers 5 bags one way")

    assert flow.menu.kind == "category"
    assert [c["name"] for c in flow.menu.options] == ["Full Size SUV XL"]
    text = "\n".join(out)
    assert "1. Full Size SUV XL — up to 7 pax / 7 bags — 4 available — $150.10 one way" in text
    assert "Too small for 6 passengers / 5 bags (hidden): Mid-Size Sedan X, Mid-Size SUV" in text
    assert "Classic" not in text
    assert "Water Taxi" not in text


async def test_4_category_pick_calls_vehicle_listing_with_type_and_user(out: list[str]) -> None:
    _flow, api = await to_vehicle_menu("admin", out)

    listing = api.named("vehicles")[0]
    assert listing["filters"] == {"vehicle-type": [3]}
    assert listing["user_id"] == USER_ID
    assert listing["selected_amenities"] == []
    text = "\n".join(out)
    assert (
        "1. 2024 Hyundai Tucson (Mid-Size SUV) — SKYLIMO (+1 312 555 0199) — 4 pax / 4 bags — from $104.76"
        in text
    )
    assert "from $124.36" in text


async def test_5_round_trip_uses_round_trip_totals_and_collects_return(out: list[str]) -> None:
    flow, api = make_flow("admin", out)
    await flow.start("5 am pickup on 14 nov from ord to mdw round trip, 2 people, 1 bag")
    asked = [flow.pending, *await run(flow, "16 nov", "7 pm", "yes")]

    assert asked == ["return_date", "return_time", "return_route", "category"]
    assert "Mid-Size SUV — up to 4 pax / 4 bags — 2 available — $209.52 round trip" in "\n".join(out)
    quote = api.named("master")[0]
    assert len(quote["location_info"]) == 2  # the backend reads [0] and [1]
    assert (quote["return_pickup_airport"], quote["return_dropoff_airport"]) == (117, 2)
    assert quote["return_pickup_address"] == quote["return_pickup_airport_name"]  # consistent leg
    await run(flow, "2", "1")
    assert flow.d.return_rates is not None
    assert "$209.52" in "\n".join(out)


async def test_6_airport_arrival_asks_flight_details_before_confirm(out: list[str]) -> None:
    flow, _ = await to_vehicle_menu("individual", out)
    asked = await run(flow, "1", "aa", "AA 1234", "New York", "ok", "UA")

    assert asked == [
        "airline_search:0:pickup",
        "detail:0:pickup_flight",
        "detail:0:city",
        "meet_default:0",
        "airline_search:0:dropoff",
        "detail:0:dropoff_flight",
    ]
    leg = flow.d.outbound
    assert (leg.pickup_flight.airline["code"], leg.pickup_flight.number, leg.pickup_flight.city) == (
        "AA",
        "AA 1234",
        "New York",
    )
    assert await run(flow, "skip") == ["confirm"]


async def test_7_admin_chooses_existing_customer_from_a_menu(out: list[str]) -> None:
    flow, api = await to_vehicle_menu("admin", out)
    assert await run(flow, "1") == ["customer_kind"]
    text = "\n".join(out)
    assert "1. An existing customer" in text
    assert "3. A new customer" in text

    assert await run(flow, "1", "asha") == ["customer_search", "account:customer"]
    assert [a.label for a in flow.menu.options] == [
        "Asha Rider (+1 3125550111)",
        "Asha Patel (+1 3125550112)",
    ]
    await flow.handle("1")
    assert ("GET", "get-an-account/900") in api.calls
    assert (flow.d.customer.account_type, flow.d.customer.acc_id) == ("individual", 900)
    assert flow.d.passenger.name == "Asha Rider"


async def test_8_travel_agent_picks_a_client(out: list[str]) -> None:
    flow, api = await to_vehicle_menu("travel_agent", out)
    await run(flow, "1", "1", "bob", "1")
    await run(flow, "AA", "AA 1", "Boston", "ok", "UA", "skip")
    assert flow.pending == "confirm"
    await flow.handle("yes")

    path, body = next(c for c in api.calls if c[0].endswith("create-reservation"))
    assert path == "travel-planner/create-reservation"
    assert (body["account_type"], body["travel_client_acc"], body["travel_client_id"]) == (
        "travel_planner",
        "travel_individual",
        902,
    )
    assert body["acc_id"] == ""  # set by the backend from the token
    assert "travelAgentShare" in body["shares_array"]


async def test_9_no_at_confirm_submits_nothing(out: list[str]) -> None:
    flow, api = await to_vehicle_menu("individual", out)
    await run(flow, "1", "AA", "AA 1", "Boston", "ok", "UA", "skip")
    assert flow.pending == "confirm"
    await flow.handle("no")

    assert not [c for c in api.calls if c[0].endswith("create-reservation")]
    assert "Not booked" in out[-1]
    assert flow.pending is None
    assert not flow.finished


async def test_10_submit_error_is_explained_and_never_retried(out: list[str]) -> None:
    api = FakeApi(
        create_error=ApiError(
            "Validation Error",
            status=422,
            errors={"passenger_cell": "The provided phone number is not valid."},
        )
    )
    flow, _ = await to_vehicle_menu("individual", out, api)
    await run(flow, "1", "AA", "AA 1", "Boston", "ok", "UA", "skip", "yes")

    creates = [c for c in api.calls if c[0].endswith("create-reservation")]
    assert len(creates) == 1
    assert any("not created: passenger cell: The provided phone number is not valid." in line for line in out)
    await flow.handle("hello?")  # anything that is not a fix or a new yes sends nothing
    assert len([c for c in api.calls if c[0].endswith("create-reservation")]) == 1
    assert not flow.finished


async def test_11_change_passengers_recalls_the_listings(out: list[str]) -> None:
    flow, api = await to_vehicle_menu("admin", out)
    assert (len(api.named("master")), len(api.named("vehicles"))) == (1, 1)

    await flow.handle("change passengers to 3")

    assert (len(api.named("master")), len(api.named("vehicles"))) == (2, 2)
    assert api.named("master")[1]["no_of_passenger"] == 3
    assert flow.menu.kind == "vehicle"


# --- more of the flow -----------------------------------------------------------------


async def test_individual_does_not_see_operator_or_driver_contact(out: list[str]) -> None:
    flow, _ = await to_vehicle_menu("individual", out)
    await run(flow, "1", "AA", "AA 1", "Boston", "ok", "UA", "skip")
    text = "\n".join(out)
    assert "SKYLIMO" not in text
    assert "312 555 0199" not in text
    assert "joe@example.com" not in text
    assert "3125550100" not in text
    assert "Driver:     Joseph Anselmo, male" in text


async def test_admin_summary_shows_driver_contact(out: list[str]) -> None:
    flow, _ = await to_vehicle_menu("admin", out)
    await run(flow, "1", "1", "bob", "1", "AA", "AA 1", "Boston", "ok", "UA", "skip")
    assert flow.pending == "confirm"
    assert "Joseph Anselmo, male — 1 3125550100 joe@example.com" in "\n".join(out)


async def test_individual_booking_payload(out: list[str]) -> None:
    flow, api = await to_vehicle_menu("individual", out)
    await run(flow, "1", "AA", "AA 1", "Boston", "ok", "UA", "skip", "yes")

    path, body = next(c for c in api.calls if c[0].endswith("create-reservation"))
    assert path == "individual/create-reservation"
    assert "booking_created_from" not in body  # only subscribers send it
    assert (body["service_type"], body["transfer_type"]) == ("one_way", "airport_to_airport")
    assert (body["pickup_airport"], body["dropoff_airport"]) == (2, 117)
    assert body["pickup_latitude"] == body["pickup_airport_latitude"]  # mirrored for the backend
    assert (body["pickup_airline"], body["pickup_airline_name"]) == (
        1,
        "AA - American Airlines, United States",
    )
    assert (body["pickup_flight"], body["origin_airport_city"]) == ("AA 1", "Boston")
    assert (body["vehicle_id"], body["vehicle_type"], body["affiliate_id"]) == (501, 3, 31)
    assert (body["driver_name"], body["driver_cell_isd"]) == ("Joseph Anselmo", "+1")
    assert (body["passenger_name"], body["passenger_cell"]) == ("Asha Rider", "3125550111")
    assert (body["grand_total"], body["sub_total"]) == (128.0, 128.0)
    assert "grand_total" not in body["rateArray"]
    assert body["currency"] == "USD"
    assert body["platform_type"] == "web"
    assert body["proceed"] is True
    assert "  Booked. Reservation #12345 (order ORD-1)." in out
    assert flow.finished


async def test_past_time_is_refused_for_non_admins(out: list[str]) -> None:
    flow, _ = make_flow("individual", out)
    await flow.start("today at 6 am from ord to mdw one way 1 passenger 0 bags")
    assert flow.pending == "time"
    assert any("is in the past" in line for line in out)


async def test_relative_date_is_confirmed(out: list[str]) -> None:
    flow, _ = make_flow("admin", out)
    await flow.start("kal subah 5 baje ORD se MDW")
    assert flow.pending == "relative"
    assert "'kal' = Tue 27 Oct 2026. Right? (yes/no)" in out[-1]
    assert flow.d.outbound.time == "05:00:00"


async def test_cancel_books_nothing(out: list[str]) -> None:
    flow, api = await to_vehicle_menu("admin", out)
    await flow.handle("cancel")
    assert flow.finished
    assert not [c for c in api.calls if c[0].endswith("create-reservation")]


async def test_back_goes_from_vehicles_to_categories(out: list[str]) -> None:
    flow, _ = await to_vehicle_menu("admin", out)
    await flow.handle("back")
    assert flow.menu.kind == "category"


# --- parser ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5 am pickup on 14 nov from ord to mdw", (date(2026, 11, 14), "05:00:00", "ord", "mdw")),
        ("kal subah 5 baje ORD se MDW", (date(2026, 10, 27), "05:00:00", "ORD", "MDW")),
        (
            "parso shaam 7 baje Mumbai airport se Bandra tak",
            (date(2026, 10, 28), "19:00:00", "Mumbai airport", "Bandra"),
        ),
        (
            "from 233 S Wacker Dr to ORD on 2026-12-01 at 17:30",
            (date(2026, 12, 1), "17:30:00", "233 S Wacker Dr", "ORD"),
        ),
        ("nov 3 at 12 pm JFK to LGA", (date(2026, 11, 3), "12:00:00", "JFK", "LGA")),
    ],
)
def test_parse_trip(text: str, expected: tuple) -> None:
    parsed = parse_trip(text, date(2026, 10, 26))
    assert (
        parsed.date,
        parsed.time.value() if parsed.time else None,
        parsed.pickup,
        parsed.dropoff,
    ) == expected


def test_parse_counts_and_service() -> None:
    parsed = parse_trip("round trip, 3 log, 2 bag", date(2026, 10, 26))
    assert (parsed.service_type, parsed.pax, parsed.luggage) == ("round_trip", 3, 2)
    charter = parse_trip("charter for 4 hours from navy pier", date(2026, 10, 26))
    assert (charter.service_type, charter.hours, charter.pickup) == ("charter_tour", 4, "navy pier")


def test_parse_past_date_rolls_to_next_year() -> None:
    assert parse_trip("on 3 jan", date(2026, 10, 26)).date == date(2027, 1, 3)


def test_parse_ambiguous_hour() -> None:
    parsed = parse_trip("at 5 from ord to mdw", date(2026, 10, 26))
    assert parsed.time is not None
    assert parsed.time.ambiguous


# --- rates: the web app's totals and shares ------------------------------------------


def test_outbound_rates_match_web_math() -> None:
    rates = outbound_rates(
        RATE_ARRAY,
        service_type="one_way",
        hours=2,
        min_rate_involved=False,
        flags=ShareFlags(is_travel_share=False, is_created_by_admin=True),
    )
    # Base_Rate total = 80 + admin share (25 % of 80 + 25 % of the 16 gratuity) = 104
    assert rates.totals == {
        "Base_Rate": 104.0,
        "ELH_Charges": 0,
        "Stops": 0,
        "Wait": 0,
        "Trip_Tax": 8.0,
        "Extra_Gratuity": 16.0,
    }
    assert (rates.sub_total, rates.grand_total) == (128.0, 128.0)
    assert rates.lines["taxes"]["Trip_Tax"]["amount"] == 8.0  # 10 % of the all-inclusive sum
    shares = share_array(
        rates,
        service_type="one_way",
        hours=2,
        account_type="individual",
        flags=ShareFlags(is_travel_share=False, is_created_by_admin=True),
    )
    assert shares == {
        "baseRate": 80,
        "grandTotal": 128.0,
        "stripeFee": pytest.approx(6.7),
        "adminShare": 24.0,
        "deducted_admin_share": pytest.approx(17.3),
        "affiliateShare": 108.0,
    }


def test_charter_rates_multiply_base_rate_by_hours() -> None:
    rates = outbound_rates(
        RATE_ARRAY,
        service_type="charter_tour",
        hours=3,
        min_rate_involved=False,
        flags=ShareFlags(is_travel_share=False, is_created_by_admin=True),
    )
    assert rates.lines["all_inclusive_rates"]["Base_Rate"]["amount"] == 240.0
    # 240 + 25 % of 240 + 4 = 304; tax 10 % of 240 = 24; gratuity 16
    assert rates.grand_total == 344.0


def test_travel_agent_rates_and_shares() -> None:
    flags = ShareFlags(is_travel_share=True, is_created_by_admin=False)
    rates = outbound_rates(RATE_ARRAY, service_type="one_way", hours=2, min_rate_involved=False, flags=flags)
    assert rates.totals["Base_Rate"] == 104.0  # 80 + 15 % admin + 4 + 10 % agent
    shares = share_array(rates, service_type="one_way", hours=2, account_type="travel_planner", flags=flags)
    assert (shares["adminShare"], shares["travelAgentShare"]) == (12.0, 8.0)
    back = return_rates(RATE_ARRAY, service_type="round_trip", hours=2, flags=flags)
    assert back.grand_total == 128.0


def test_rates_refuse_what_the_web_app_cannot_price() -> None:
    no_gratuity = {**RATE_ARRAY, "misc": {}}
    with pytest.raises(RatesError, match="Extra_Gratuity"):
        outbound_rates(
            no_gratuity,
            service_type="one_way",
            hours=2,
            min_rate_involved=False,
            flags=ShareFlags(is_travel_share=False, is_created_by_admin=True),
        )


def test_to_fixed2_rounds_like_javascript() -> None:
    assert to_fixed2(1.005) == 1.0  # 1.005 is 1.00499999... in binary, as in JS
    assert to_fixed2(0.125) == 0.13  # an exact half goes up (Python's round() gives 0.12)


# --- payload and reference data ------------------------------------------------------


def test_one_way_quote_payload_leaves_return_fields_empty() -> None:
    ord_place = Place("airport", "ORD", 41.98, -87.9, airport_id=2, airport_label="ORD")
    wacker = Place("city", "233 S Wacker Dr", 41.87, -87.63)
    d = BookingDraft(
        service_type="one_way",
        pax=2,
        luggage=1,
        outbound=Leg(
            date=date(2026, 11, 14), time="05:00:00", pickup=wacker, dropoff=ord_place, route=Route(1000, 600)
        ),
    )
    quote = quote_payload(d, form_value(d, mode="admin"))
    assert (quote["pickup_type"], quote["dropoff_type"]) == ("city", "airport")
    assert (quote["pickup_airport"], quote["pickup_address"], quote["pickup_address_lat"]) == (
        None,
        "233 S Wacker Dr",
        41.87,
    )
    assert quote["return_pickup_address"] == quote["return_dropoff_airport"] == ""
    assert quote["booking_hour"] == "2"
    assert len(quote["location_info"]) == 1


def test_airport_index_and_currency() -> None:
    airports = airport_index()
    assert airports.by_code("ord").id == 2
    assert [a.code for a in airports.in_city("Chicago")] == ["ORD", "MDW"]  # FBO excluded
    assert airports.nearest(41.786, -87.752).code == "MDW"
    assert currency_for(["Chicago", "United States"])["currency"] == "USD"


def test_card_validation_follows_the_web_form() -> None:
    good = {
        "name": "A B",
        "number": "4242 4242 4242 4242",
        "cvc": "123",
        "exp_month": "12",
        "exp_year": "2030",
    }
    assert validate_card(good, date(2026, 10, 26)) is None
    assert "12 to 20" in validate_card({**good, "number": "4242"}, date(2026, 10, 26))
    assert "expired" in validate_card({**good, "exp_year": "2026", "exp_month": "9"}, date(2026, 10, 26))


# --- the HTTP client ------------------------------------------------------------------


@respx.mock
async def test_limo_api_sends_bearer_and_reads_laravel_errors() -> None:
    route = respx.post("https://api.test/api/individual/create-reservation").mock(
        return_value=httpx.Response(
            422, json={"success": False, "message": "Validation Error", "data": {"pickup_date": "Required."}}
        )
    )
    async with httpx.AsyncClient() as client:
        api = LimoApi("https://api.test/api/", "secret-token", client)
        with pytest.raises(ApiError) as caught:
            await api.post("individual/create-reservation", {"a": 1})
    assert route.calls.last.request.headers["Authorization"] == "Bearer secret-token"
    assert caught.value.plain() == "pickup date: Required."
    assert "secret-token" not in repr(api)
    assert "secret-token" not in str(caught.value)


@respx.mock
async def test_limo_api_network_failure_is_unreachable() -> None:
    respx.post("https://api.test/api/create-reservation").mock(side_effect=httpx.ConnectTimeout("boom"))
    async with httpx.AsyncClient() as client:
        api = LimoApi("https://api.test/api/", "secret-token", client)
        with pytest.raises(ApiUnreachableError):
            await api.post("create-reservation", {})


async def test_card_form_posts_once_and_never_echoes_the_number() -> None:
    import threading

    sent: list[dict[str, Any]] = []
    pages: list[str] = []

    async def submit(fields: dict[str, Any]) -> str:
        sent.append(fields)
        return "Credit card has been added successfully."

    def browser(url: str) -> None:
        def post() -> None:
            form = {
                "name": "Asha Rider",
                "number": "4242 4242 4242 4242",
                "cvc": "123",
                "exp_month": "7",
                "exp_year": "2030",
            }
            pages.append(httpx.post(url, data=form, timeout=10).text)

        threading.Thread(target=post, daemon=True).start()

    lines: list[str] = []
    result = await collect_card(submit, title="t", say=lines.append, open_browser=browser, wait_seconds=10)

    assert result.added
    assert sent == [
        {
            "card_type": "personal",
            "name": "Asha Rider",
            "number": "4242424242424242",
            "cvc": "123",
            "exp_month": "07",
            "exp_year": 2030,
        }
    ]
    assert all("4242" not in line for line in lines)
    assert "4242" not in pages[0]
