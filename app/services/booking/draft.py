"""What the conversation has collected so far: the shell's equivalent of the booking form."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from app.services.booking.maps import Route
from app.services.booking.rates import Rates

PlaceKind = Literal["city", "airport", "cruise"]


@dataclass(frozen=True, slots=True)
class Place:
    """One resolved endpoint or stop. ``kind`` is the half of ``transfer_type`` it produces."""

    kind: PlaceKind
    address: str  # display / formatted address (``pickup`` etc.)
    lat: float
    lng: float
    airport_id: int | str = ""  # internal airport id (airports.json) for airports
    airport_label: str = ""  # ``*_airport_option`` / ``*_airport_name``
    country_names: tuple[str, ...] = ()

    @property
    def short(self) -> str:
        return self.airport_label or self.address

    def same_spot(self, other: Place) -> bool:
        if self.address and self.address.strip().lower() == other.address.strip().lower():
            return True
        return round(self.lat, 5) == round(other.lat, 5) and round(self.lng, 5) == round(other.lng, 5)


@dataclass
class Flight:
    airline: dict[str, Any] | None = None  # airlinesData item (the ng-select value)
    number: str = ""
    city: str = ""  # origin_airport_city (outbound pickup) / departing_airport_city (return pickup)
    number_skipped: bool = False  # a departure flight number is optional


@dataclass
class Cruise:
    port: str = ""
    name: str = ""
    time: str = "12:00 am"  # form default; the web form sends it untouched when not picked


@dataclass
class Leg:
    date: date | None = None
    time: str | None = None  # HH:MM:SS, a 15-minute step (constant_data.time_values)
    pickup: Place | None = None
    dropoff: Place | None = None
    stops: list[Place] = field(default_factory=list)
    stops_answered: bool = False
    route: Route | None = None
    pickup_flight: Flight = field(default_factory=Flight)
    dropoff_flight: Flight = field(default_factory=Flight)
    cruise: Cruise = field(default_factory=Cruise)
    meet_greet: dict[str, Any] | None = None  # {"id", "message"}; None = the form default

    @property
    def transfer_type(self) -> str:
        if self.pickup is None or self.dropoff is None:
            return ""
        return f"{self.pickup.kind}_to_{self.dropoff.kind}"

    def reset_route(self) -> None:
        self.route = None


@dataclass
class Customer:
    """Who the booking belongs to (BOOKING_FLOW.md §2)."""

    account_type: str  # individual | travel_planner | loose_customer
    label: str = ""
    acc_id: int | str = ""
    travel_client_acc: str = "travel_individual"  # or travel_loose_customer
    travel_client_id: int | str = ""
    loose: dict[str, str] | None = None  # first_name, last_name, email, phone, phone_isd, phone_country

    @property
    def is_new(self) -> bool:
        return self.loose is not None


@dataclass
class Passenger:
    name: str = ""
    email: str = ""
    cell: str = ""
    cell_isd: str = "+1"
    cell_country: str = "us"


@dataclass
class BookingDraft:
    service_type: str | None = None  # one_way | round_trip | charter_tour
    hours: int = 2  # number_of_hours; the minimum for a charter is 2
    outbound: Leg = field(default_factory=Leg)
    ret: Leg = field(default_factory=Leg)
    return_route_confirmed: bool = False
    pax: int | None = None
    luggage: int | None = None
    auto_charter_note: bool = False  # set when > 2 stops turned the booking into a charter

    category: dict[str, Any] | None = None
    vehicle: dict[str, Any] | None = None  # vehicle-listing item, or the master category itself
    master_info: dict[str, Any] | None = None  # get-master-vehicle-info (admin, master vehicles)
    currency_symbol: str = "$"
    rates: Rates | None = None
    return_rates: Rates | None = None

    customer: Customer | None = None
    passenger: Passenger | None = None
    instructions: str = ""  # extra notes, added to booking_instructions

    @property
    def is_round_trip(self) -> bool:
        return self.service_type == "round_trip"

    @property
    def is_charter(self) -> bool:
        return self.service_type == "charter_tour"

    @property
    def legs(self) -> list[Leg]:
        return [self.outbound, self.ret] if self.is_round_trip else [self.outbound]

    @property
    def is_master_vehicle(self) -> bool:
        return bool(self.vehicle and self.vehicle.get("is_master_vehicle"))

    def clear_pricing(self) -> None:
        """Trip details changed: every listing and price must be fetched again."""
        self.category = self.vehicle = self.master_info = None
        self.rates = self.return_rates = None
        for leg in (self.outbound, self.ret):
            leg.reset_route()
