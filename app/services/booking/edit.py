"""Changing a saved booking: load it, apply what the user asks, show old -> new, save once.

Rules follow EDIT_BOOKING_FLOW.md. The booking is loaded with the role's
``get-reservation/{id}/edit`` and turned into a draft, so the booking flow's
``change ... to ...``, place lookups, checks, vehicle lists and rates work as they do for a new
booking. What is sent back is the loaded booking in the web form's shape, with only the fields
the changes touched replaced: the web app also resubmits its whole prefilled form.

At any point the user can say:
    change time to 6 pm | change pickup to ORD | change passengers to 3 | add stop <address>
    change customer   (admins, travel agents) who the booking is for
    vehicles      pick another vehicle
    reprice       (admins) use the system price instead of the saved one
    undo          revert the last change
    summary       show the booking as it is now
    cancel        stop without saving
Nothing is saved until the list of changes is shown and the user answers yes.

A new customer (or a travel agent's new client) needs a card, which the edit API takes with the
customer. The flow never sees it: in the web chat it hands the finished request to the chat
widget (``card_request``), whose card form adds the card and sends it to the API itself, then
reports back with ``card:ok`` / ``card:error <message>``. The shell offers no new customers.
"""

from __future__ import annotations

import copy
import html
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.services.booking.api import ApiError, ApiUnreachableError
from app.services.booking.draft import BookingDraft, Cruise, Customer, Flight, Leg, Passenger, Place
from app.services.booking.flow import (
    _SERVICE_NAMES,
    BookingFlow,
    Menu,
    _Account,
    _account_label,
    _change_parts,
    _clock,
    _day,
    _float,
    _int,
    _money,
    _plain,
)
from app.services.booking.lookup import LOCKED_STATUSES, BookingRow, RelatedLeg, ReservationLookup
from app.services.booking.maps import Route
from app.services.booking.parse import parse_service, parse_yes_no
from app.services.booking.payload import (
    airline_label,
    apply_rates,
    booking_currency,
    default_instructions,
    empty_form,
    finish_form,
    form_value,
)
from app.services.booking.rates import RatesError, ShareFlags, outbound_rates, share_array
from app.services.booking.reference import AirportIndex, currency_symbol

logger = get_logger(__name__)

# "edit booking 2141", "change my booking", "reschedule my ride", "booking 2141 ka time badalna hai"
EDIT_REQUEST = re.compile(
    r"\b(?:edit|change|modify|update|amend|reschedule|badal\w*|badlo)\b.*\b(?:bookings?|reservations?|"
    r"rides?|trips?|#?\d{3,})\b|"
    r"\b(?:bookings?|reservations?|rides?|trips?)\b.*\b(?:edit|change|modify|update|amend|reschedule|"
    r"badal\w*|badlo)\b",
    re.IGNORECASE,
)
# Any change request, for a conversation that has just booked or edited a booking.
_EDIT_VERB = re.compile(r"\b(?:edit|change|modify|update|amend|correct|fix|switch|replace|reschedule|"
                        r"badal\w*|badlo)\b", re.IGNORECASE)  # fmt: skip
_CUSTOMER_INTENT = re.compile(
    r"\b(?:change|update|edit|modify|switch|replace)\b.*\b(?:customer|client|cliet|costumer|account)\b",
    re.IGNORECASE,
)
_QUESTION = re.compile(r"^\s*(?:how|what|why|when|where|which|who|is|are|does|do)\b", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![\d.:])#?(\d{3,9})(?![\d.:])")
_CANCEL = ("cancel", "stop", "quit", "exit", "rehne do", "chhodo", "leave it", "never mind", "nevermind")
_VEHICLES = ("vehicles", "show vehicles", "other vehicles", "show other vehicles", "categories",
             "change vehicle", "change the vehicle", "another vehicle")  # fmt: skip
# Sent from the draft only when the customer was changed (EDIT_BOOKING_FLOW.md §5).
_CUSTOMER_KEYS = ("account_type", "acc_id", "travel_client_acc", "travel_client_id", "loose_customer",
                  "sub_account_type", "sub_account_id", "change_individual_data")  # fmt: skip
_VEHICLE_KEY_PREFIXES = ("affiliate_", "vehicle_", "driver_", "lose_affiliate_", "loose_affiliate_id",
                         "is_old_loose_affiliate")  # fmt: skip
_ROUND_TRIP_REFUSAL = (
    "  This booking is a single ride. To add a return trip, book it as a new ride "
    "(say 'book a ride ...'); changing it here would create a second booking."
)


def is_edit_request(text: str) -> bool:
    """A message asking to change a booking (not a question about how to)."""
    return EDIT_REQUEST.search(text) is not None and _QUESTION.match(text) is None


def is_edit_follow_up(text: str) -> bool:
    """Any change request ("update passenger details", "change the customer"). Only used right
    after a booking was made or edited in the same conversation."""
    return _EDIT_VERB.search(text) is not None and _QUESTION.match(text) is None


def booking_number(text: str) -> int | None:
    match = _NUMBER.search(text)
    return int(match.group(1)) if match else None


# --- loaded booking -> form value and draft ------------------------------------------------


def form_from_reservation(
    data: dict[str, Any], *, mode: str, airlines: list[dict[str, Any]], meet_greets: list[dict[str, Any]]
) -> dict[str, Any]:
    """``prefillViaBookingID`` (booking.ts:3458-3800): the booking copied into the form's controls."""
    v = empty_form(mode)
    for key, value in data.items():
        # the web loop copies truthy scalars onto controls with the same name
        if key in v and value and not isinstance(value, dict | list):
            v[key] = value
    isd = str(data.get("passenger_cell_isd") or "")
    if isd:
        v["passenger_cell_isd"] = isd if "+" in isd else f"+{isd}"
    v["service_type"] = _service(data.get("service_type"))
    hours = _int(data.get("number_of_hours"))
    v["number_of_hours"] = hours or 2
    v["pickup_date"] = str(data.get("pickup_date") or v["pickup_date"])[:10]
    v["pickup_time"] = _time_value(data.get("pickup_time")) or v["pickup_time"]
    v["cancellation_hours"] = str(data.get("cancellation_hours") or "24")
    v["booking_instructions"] = str(data.get("booking_instructions") or "").replace("<br />", "")
    if v["account_type"] == "loose_customer":  # a loose customer is saved as an individual account
        v["account_type"] = "individual"
    for prefix in ("", "return_"):
        for end in ("pickup", "dropoff"):
            key = f"{prefix}{end}"
            line = data.get(key) or data.get(f"{key}_address")
            if line:
                v[key] = line
            if data.get(f"{key}_airport"):
                label = str(data.get(f"{key}_address") or line or "")
                v[f"{key}_airport_option"] = v[f"{key}_airport_name"] = label
        for end in ("pickup", "dropoff"):
            key = f"{prefix}{end}_airline"
            airline = _airline(airlines, data.get(key))
            if airline is not None:
                v[f"{key}_option"], v[f"{key}_name"] = airline, airline_label(airline)
    v["origin_airport_city"] = data.get("origin_airport_city") or data.get("departing_airport_city") or ""
    v["meet_greet_choices_name"] = (
        data.get("meet_greet_choice_name")
        or _meet_greet_message(meet_greets, data.get("meet_greet_choices"))
        or v["meet_greet_choices_name"]
    )
    for key in ("extra_stops", "return_extra_stops", "driver_languages", "driver_dresses",
                "amenities", "chargedAmenities"):  # fmt: skip
        v[key] = _json_list(data.get(key))
    if data.get("distance"):
        v["journeyDistance"], v["journeyTime"] = data.get("distance"), data.get("duration") or ""
    if _truthy(data.get("is_pending_affiliate")):
        v["affiliate_type"] = "in_progress_affiliate"
    if _truthy(data.get("return_is_pending_affiliate")):
        v["return_affiliate_type"] = "in_progress_affiliate"
    for image in ("driver_image", "vehicle_image"):
        if isinstance(data.get(image), dict) and data[image].get("id"):
            v[f"{image}_id"] = data[image]["id"]
    v["reservation_id"] = data.get("reservation_id") or data.get("id") or ""
    v["updateType"] = "edit"
    v["prevent_rate_override"] = mode == "admin"
    return v


def draft_from_reservation(
    data: dict[str, Any],
    *,
    airports: AirportIndex,
    airlines: list[dict[str, Any]],
    meet_greets: list[dict[str, Any]],
    vehicle_categories: list[dict[str, Any]],
) -> BookingDraft:
    """The booking as the draft the booking flow works on. Raises ValueError if it can't be read."""
    kinds = str(data.get("transfer_type") or "city_to_city").split("_to_")
    if len(kinds) != 2:
        raise ValueError(f"unknown transfer type {data.get('transfer_type')!r}")
    d = BookingDraft(service_type=_service(data.get("service_type")))
    d.hours = _int(data.get("number_of_hours")) or 2
    leg = d.outbound
    leg.date = date.fromisoformat(str(data.get("pickup_date") or "")[:10])
    leg.time = _time_value(data.get("pickup_time"))
    leg.pickup = _end(data, "pickup", kinds[0], airports)
    leg.dropoff = _end(data, "dropoff", kinds[1], airports)
    if leg.pickup is None or leg.dropoff is None:
        raise ValueError("the booking has no pickup or drop-off")
    leg.stops = [_stop(s) for s in _json_list(data.get("extra_stops")) if isinstance(s, dict)]
    leg.stops_answered = True
    meters, seconds = _int(data.get("distance")), _int(data.get("duration"))
    if meters and meters > 0:
        leg.route = Route(meters=meters, seconds=seconds or 0)
    leg.pickup_flight = Flight(
        airline=_airline(airlines, data.get("pickup_airline")),
        number=str(data.get("pickup_flight") or ""),
        city=str(data.get("origin_airport_city") or data.get("departing_airport_city") or ""),
    )
    departing = str(data.get("dropoff_flight") or "")
    leg.dropoff_flight = Flight(
        airline=_airline(airlines, data.get("dropoff_airline")),
        number=departing,
        number_skipped=not departing,
    )
    leg.cruise = Cruise(
        port=str(data.get("cruise_port") or ""),
        name=str(data.get("cruise_name") or ""),
        time=str(data.get("cruise_time") or "12:00 am").lower(),
    )
    if data.get("meet_greet_choices"):
        message = data.get("meet_greet_choice_name") or _meet_greet_message(
            meet_greets, data.get("meet_greet_choices")
        )
        leg.meet_greet = {"id": _int(data["meet_greet_choices"]), "message": message or ""}
    d.pax = _int(data.get("total_passengers")) or 1
    d.luggage = _int(data.get("luggage_count")) or 0
    type_name = _category_name(vehicle_categories, data.get("vehicle_type")) or "the booked vehicle"
    d.category = {"name": type_name}
    d.vehicle = _saved_vehicle(data, type_name)
    d.currency_symbol = currency_symbol(data.get("currency")) or "$"
    isd = str(data.get("passenger_cell_isd") or "+1")
    d.passenger = Passenger(
        name=str(data.get("passenger_name") or ""),
        email=str(data.get("passenger_email") or ""),
        cell=re.sub(r"[^\d+]", "", str(data.get("passenger_cell") or "")),
        cell_isd=isd if isd.startswith("+") else f"+{isd}",
        cell_country=str(data.get("passenger_cell_country") or "us"),
    )
    owner = data.get("loose_customer") if isinstance(data.get("loose_customer"), dict) else {}
    label = " ".join(str(owner.get(k) or "") for k in ("first_name", "last_name")).strip()
    account_type = str(data.get("account_type") or "individual")
    if account_type == "travel_planner" and data.get("travel_client_id"):
        label = f"a client of {label or 'a travel agent'}"  # the client's own name is loaded by EditFlow
    label = label or d.passenger.name or f"account {data.get('acc_id')}"
    d.customer = Customer(
        "individual" if account_type == "loose_customer" else account_type,
        label=label,
        acc_id=data.get("acc_id") or "",
        travel_client_acc="travel_individual",
        travel_client_id=data.get("travel_client_id") or "",
    )
    return d


def _service(value: Any) -> str:
    # both legs of a round trip are saved as 'oneway' rows; an edit changes one leg
    return "charter_tour" if str(value or "") in ("chartertour", "charter_tour") else "one_way"


def _time_value(value: Any) -> str | None:
    """'17:30:00', '17:30' or '05:30 PM' -> '17:30:00'."""
    text = str(value or "").strip()
    for pattern in ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(text.upper(), pattern).strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


def _end(data: dict[str, Any], key: str, kind: str, airports: AirportIndex) -> Place | None:
    line = str(data.get(key) or data.get(f"{key}_address") or "")
    if kind == "airport":
        airport = airports.by_id(data.get(f"{key}_airport"))
        lat = _float(data.get(f"{key}_airport_latitude")) or (airport.lat if airport else None)
        lng = _float(data.get(f"{key}_airport_longitude")) or (airport.long if airport else None)
        label = str(data.get(f"{key}_address") or "") or (airport.label if airport else line)
        if not label:
            return None
        return Place(
            kind="airport",
            address=label,
            lat=lat or _float(data.get(f"{key}_latitude")) or 0.0,
            lng=lng or _float(data.get(f"{key}_longitude")) or 0.0,
            airport_id=data.get(f"{key}_airport") or "",
            airport_label=label,
            country_names=(airport.country,) if airport else (),
        )
    if not line:
        return None
    return Place(
        kind="cruise" if kind == "cruise" else "city",
        address=line,
        lat=_float(data.get(f"{key}_latitude")) or 0.0,
        lng=_float(data.get(f"{key}_longitude")) or 0.0,
    )


def _stop(stop: dict[str, Any]) -> Place:
    airport = stop.get("kind") == "airport" and stop.get("airport")
    return Place(
        kind="airport" if airport else "city",
        address=str(stop.get("address") or ""),
        lat=_float(stop.get("latitude")) or 0.0,
        lng=_float(stop.get("longitude")) or 0.0,
        airport_id=stop.get("airport") or "" if airport else "",
        airport_label=str(stop.get("airport_name") or "") if airport else "",
    )


def _saved_vehicle(data: dict[str, Any], type_name: str) -> dict[str, Any]:
    """The booked vehicle and driver in the shape of a vehicle-listing row (for the summary and
    the rates call)."""

    def name(key: str) -> str:
        value = data.get(f"{key}_name") or data.get(key)
        return "" if value in (None, "") or str(value).isdigit() else str(value)  # ids are not names

    isd = str(data.get("driver_cell_isd") or "")
    return {
        "id": data.get("vehicle_id") or "",
        "affiliate_id": data.get("affiliate_id") or "",
        "vehicle_type_id": data.get("vehicle_type") or "",
        "name": type_name,
        "passenger": _int(data.get("vehicle_seats")),
        "is_master_vehicle": False,
        "driverInformation": {
            "id": data.get("driver_id") or "",
            "name": str(data.get("driver_name") or ""),
            "gender": str(data.get("driver_gender") or ""),
            "email": str(data.get("driver_email") or ""),
            "cell_number": str(data.get("driver_cell") or ""),
            "cell_isd": isd if not isd or isd.startswith("+") else f"+{isd}",
            "cell_country": str(data.get("driver_cell_country") or "us"),
        },
        "vehicle_details": {
            "year": name("vehicle_year"),
            "make": name("vehicle_make"),
            "model": name("vehicle_model"),
        },
    }


def _airline(airlines: list[dict[str, Any]], airline_id: Any) -> dict[str, Any] | None:
    if airline_id in (None, "", 0, "0"):
        return None
    found = next((a for a in airlines if str(a.get("id")) == str(airline_id)), None)
    return found or {"id": airline_id, "code": "", "name": f"airline {airline_id}", "country": ""}


def _meet_greet_message(meet_greets: list[dict[str, Any]], choice: Any) -> str:
    found = next((m for m in meet_greets if str(m.get("id")) == str(choice)), None)
    return str(found.get("message") or "") if found else ""


def _category_name(categories: list[dict[str, Any]], type_id: Any) -> str:
    found = next((c for c in categories if type_id and str(c.get("id")) == str(type_id)), None)
    return str(found.get("name") or found.get("formatted_name") or "") if found else ""


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value) if isinstance(value, list) else []


def _truthy(value: Any) -> bool:
    return value in (1, True, "1", "true")


# --- the conversation -----------------------------------------------------------------------


@dataclass
class EditFlow(BookingFlow):
    """``BookingFlow`` started from a saved booking instead of an empty draft."""

    lookup: ReservationLookup | None = None
    reservation_id: int | None = None
    status: str = ""
    _data: dict[str, Any] = field(default_factory=dict)
    _original: dict[str, Any] = field(default_factory=dict)  # the booking in form shape
    _before: BookingDraft = field(default_factory=BookingDraft)
    _saved_rates: dict[str, Any] | None = None  # admin: admin/reservation-rates/{id}
    _keep_price: bool = False
    _undo: list[BookingDraft] = field(default_factory=list)
    _route_keys: dict[int, tuple[Any, ...]] = field(default_factory=dict)
    _related: list[RelatedLeg] = field(default_factory=list)
    _proceed: bool = True
    _saved: bool = False
    _agent: _Account | None = None  # the travel agent of a travel_planner booking
    _new_passenger: Passenger | None = None  # offered with a new customer
    # The web chat's card form: the finished edit request, for the widget to add the card to.
    card_request: dict[str, Any] | None = None
    _card_request: dict[str, Any] | None = None  # kept to show the form again after a refused card

    # --- entry points -------------------------------------------------------------------

    async def start(self, text: str = "", reservation_id: int | None = None) -> None:
        """``reservation_id``: the booking this conversation just made or edited, for a follow-up
        like "change the customer" that names no booking."""
        number = reservation_id if reservation_id is not None else booking_number(text)
        if number is None:
            await self._offer_upcoming()
            return
        await self._load(number)
        follow_up = reservation_id is not None and self.reservation_id is not None and not self.finished
        if follow_up and (_change_parts(text) is not None or _CUSTOMER_INTENT.search(text)):
            await self.handle(text)  # the follow-up is the first change

    async def handle(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        if self.reservation_id is None:
            await self._choose_booking(text)
            return
        lowered = text.lower().strip(" .!")
        before = copy.deepcopy(self.d)
        await super().handle(text)
        if lowered != "undo" and self.d != before:
            self._undo.append(before)

    async def _choose_booking(self, text: str) -> None:
        lowered = text.lower().strip(" .!")
        if lowered in _CANCEL:
            self.finished = True
            self.say("  OK, no booking was changed.")
            return
        menu = self.menu
        if menu is not None and menu.kind == "edit_pick" and text.lstrip("#").isdigit():
            number = int(text.lstrip("#"))
            if 1 <= number <= len(menu.options):
                await self._load(menu.options[number - 1].id)
                return
        number = booking_number(text)
        if number is None:
            self._repeat("edit_id", "  Send the booking number (e.g. 2141), or 'cancel'.")
            return
        await self._load(number)

    async def _offer_upcoming(self) -> None:
        rows: list[BookingRow] = []
        if self.lookup is not None:
            try:
                rows = await self.lookup.upcoming(self.mode, self.user_id, self._today())
            except Exception:  # a failed lookup must not stop the user from typing the number
                logger.exception("edit_upcoming_failed")
        if not rows:
            none = "  I found no upcoming bookings that can be changed. " if self.lookup is not None else "  "
            self._ask("edit_id", none.strip() + " Which booking do you want to change? Send its number.")
            return
        self.menu = Menu("edit_pick", rows)
        self.say("  Which booking do you want to change?")
        for number, row in enumerate(rows, start=1):
            when = _at_text(row.pickup_at)
            status = f" ({_status_words(row.status)})" if self.is_admin else ""
            self.say(f"  {number}. #{row.id} — {when} — {_short(row.pickup)} → {_short(row.dropoff)}{status}")
        self.pending = "edit_id"
        self.say("  Pick a number, or type the booking number.")

    async def _load(self, reservation_id: int) -> None:
        self.menu = self.pending = None
        try:
            answer = await self.api.get(self.paths.get_reservation.format(id=reservation_id))
            data = answer.get("data")
        except ApiUnreachableError as exc:
            self._ask("edit_id", f"{exc}. Send the booking number to try again, or 'cancel'.")
            return
        except ApiError as exc:
            if exc.status not in (403, 404, 422):
                self.say(f"  Couldn't load booking #{reservation_id}: {exc.plain()}")
            data = None
        if not isinstance(data, dict) or not data:
            # one message for "doesn't exist" and "not yours", so a reply never confirms a booking exists
            self._ask("edit_id", f"I couldn't find booking #{reservation_id} on your account. Which booking?")
            return
        status = str(data.get("booking_status") or "")
        if status in LOCKED_STATUSES:
            self.say(f"  Booking #{reservation_id} is {LOCKED_STATUSES[status]}, so it can't be changed.")
            if not self.is_admin:
                self.say("  Please contact customer support if something needs fixing.")
            self.finished = True
            return
        common = await self._common_data()
        try:
            draft = draft_from_reservation(
                data,
                airports=self.airports,
                airlines=list(common.get("airlinesData") or []),
                meet_greets=list(common.get("meetGreets") or []),
                vehicle_categories=list(common.get("vehicleCategories") or []),
            )
        except (ValueError, TypeError) as exc:
            logger.warning("edit_unreadable_booking", reservation_id=reservation_id, error=str(exc))
            self.say(f"  Booking #{reservation_id} can't be changed here ({exc}). Please use the web app.")
            self.finished = True
            return
        await self._name_travel_client(draft, data)
        self.reservation_id, self.status, self._data = reservation_id, status, data
        self.d, self._before = draft, copy.deepcopy(draft)
        self._original = form_from_reservation(
            data,
            mode=self.mode,
            airlines=list(common.get("airlinesData") or []),
            meet_greets=list(common.get("meetGreets") or []),
        )
        self._route_keys = {0: _route_key(draft.outbound)}
        self._meet_greet_done = {"meet:0"}
        self._detail_skips = {"0:instructions", "0:cruise_time"}
        self._hours_confirmed = True
        if self.is_admin:
            await self._load_saved_rates()
        if self.lookup is not None:
            try:
                self._related = await self.lookup.related(reservation_id, _int(data.get("related_to")))
            except Exception:
                logger.exception("edit_related_failed")
        self.say(f"  Booking #{reservation_id} ({_status_words(status)}):")
        self._show_summary(final=False)
        self._prompt_for_change()

    async def _name_travel_client(self, draft: BookingDraft, data: dict[str, Any]) -> None:
        """A travel agent's booking: remember the agent, and name the client in the summary."""
        customer = draft.customer
        if customer is None or customer.account_type != "travel_planner":
            return
        owner = data.get("loose_customer") if isinstance(data.get("loose_customer"), dict) else {}
        agent_name = " ".join(str(owner.get(k) or "") for k in ("first_name", "last_name")).strip()
        self._agent = _Account(customer.acc_id, agent_name or f"travel agent {customer.acc_id}")
        if not customer.travel_client_id or not self.paths.account:
            return
        try:
            details = (await self.api.get(self.paths.account.format(id=customer.travel_client_id))).get(
                "data"
            )
        except ApiError:
            return
        if isinstance(details, dict) and (name := _account_label(details)):
            customer.label = name if self.mode == "travel_agent" else f"{name}, client of {self._agent.label}"

    async def _load_saved_rates(self) -> None:
        try:
            saved = await self.api.reservation_rates(self.reservation_id or 0)
        except ApiError:
            saved = {}
        if isinstance(saved.get("rateArray"), dict) and saved.get("rateArray"):
            self._saved_rates, self._keep_price = saved, True
        else:
            self.say("  (The saved price couldn't be loaded, so any change will be priced again.)")

    def _prompt_for_change(self) -> None:
        extra = ", 'change customer'" if self.mode != "individual" else ""
        extra += ", 'reprice' for the system price" if self.is_admin else ""
        self.say(
            "  What would you like to change? For example 'change time to 6 pm', 'change pickup to ORD', "
            f"'change passengers to 3', 'add stop <address>', 'change notes to ...', 'vehicles'{extra}. "
            "'cancel' leaves the booking as it is."
        )

    # --- commands -----------------------------------------------------------------------

    async def _command(self, text: str) -> bool:
        lowered = text.lower().strip(" .!")
        if lowered in _CANCEL or lowered == "cancel booking":
            self.finished = True
            self.say(
                "  Done." if self._saved else f"  Cancelled. Booking #{self.reservation_id} was not changed."
            )
            return True
        if lowered == "undo":
            self.pending = self.menu = None
            self._awaiting_confirm = False
            if self._undo:
                self.d = self._undo.pop()
                self.say("  Undone.")
            else:
                self.say("  Nothing to undo.")
            return True
        if lowered in ("summary", "show summary", "status"):
            self._show_summary(final=False)
            self._stalled = self.pending is None and self.menu is None
            return True
        if lowered in ("reprice", "system price", "use system price", "use the system price"):
            if not self.is_admin:
                self.say("  Your booking's price is always worked out again when it changes.")
            else:
                self._keep_price, self.d.rates, self.d.return_rates = False, None, None
                self.pending = self.menu = None
                self._awaiting_confirm = False
                self.say("  Using the system price for the trip as it is now.")
            return True
        if lowered in _VEHICLES:
            if self.menu is not None and self.menu.kind in ("vehicle", "category"):
                return await super()._command(text)
            self.pending, self.menu = None, None
            self._awaiting_confirm = False
            self._last_category = None
            self.d.category = self.d.vehicle = self.d.master_info = None
            self.d.rates = self.d.return_rates = None
            return True
        if lowered in ("save", "save changes", "done"):
            if self.pending == "confirm":  # an explicit answer to "Save these changes?"
                await self._answer_confirm("yes")
            return True  # otherwise the next step shows the changes and asks to save
        if _CUSTOMER_INTENT.search(text):
            self._start_customer_change()
            return True
        if re.match(r"^add\s+(?:a\s+)?return\s+stop\b", text, re.I):
            self.say(_ROUND_TRIP_REFUSAL)
            return True
        return await super()._command(text)

    async def _change(self, name: str, value: str) -> None:
        if re.search(r"\b(?:customer|account|client)\b", name):
            self._start_customer_change()
            return
        if re.search(r"\breturn\b", name):
            self.say(_ROUND_TRIP_REFUSAL)
            return
        service = parse_service(value) if re.search(r"\b(?:service|trip|type)\b", name) else None
        if service is not None and service[0] == "round_trip":
            self.say(_ROUND_TRIP_REFUSAL)
            return
        await super()._change(name, value)

    # --- changing the customer ----------------------------------------------------------

    def _start_customer_change(self) -> None:
        self.pending = self.menu = None
        self._awaiting_confirm = False
        if self.mode == "individual":
            self.say(
                "  A booking stays on your own account. You can change the passenger instead: 'change "
                "passenger name to ...', 'change passenger phone to ...', 'change passenger email to ...'."
            )
            self._stalled = True
            return
        customer = self.d.customer
        new = not self.card_form  # the card for a new customer is entered in the web chat's form
        agent = self._agent
        options: list[tuple[str, str]]
        if self.mode == "travel_agent":
            options = [("travel_individual", "Another of your clients")]
            options += [("travel_loose_customer", "A new client")] if new else []
        elif customer is not None and customer.account_type == "travel_planner" and agent is not None:
            options = [("same_agent_client", f"Another client of {agent.label}")]
            options += [("same_agent_new", f"A new client of {agent.label}")] if new else []
            options += [("travel_planner", "A client of a different travel agent"),
                        ("individual", "An existing individual customer")]  # fmt: skip
            options += [("loose_customer", "A new customer")] if new else []
        else:
            options = [
                ("individual", "Another existing customer"),
                ("travel_planner", "A travel agent's client"),
            ]
            options += [("loose_customer", "A new customer")] if new else []
        self.menu = Menu("edit_customer", [key for key, _ in options])
        now = f" (now: {customer.label})" if customer is not None else ""
        self.say(f"  Who should booking #{self.reservation_id} be for?{now}")
        for number, (_, label) in enumerate(options, start=1):
            self.say(f"  {number}. {label}")
        if not new:
            self.say(
                "  (A new customer can be added from the chat on the website, where the card is entered.)"
            )

    async def _pick(self, number: int) -> None:
        menu = self.menu
        if menu is None or menu.kind != "edit_customer" or not 1 <= number <= len(menu.options):
            await super()._pick(number)
            return
        choice = menu.options[number - 1]
        self.menu = self.pending = None
        if choice == "same_agent_client" and self._agent is not None:
            await super()._pick_account("agent", self._agent)  # lists that agent's clients
        elif choice == "same_agent_new":
            self._picked_agent = self._agent
            await self._customer_kind("travel_loose_customer")
        else:
            await self._customer_kind(choice)

    async def _pick_account(self, kind: str, account: _Account) -> None:
        customer, passenger = self.d.customer, self.d.passenger
        await super()._pick_account(kind, account)
        self._customer_picked(customer, passenger)

    def _answer_loose(self, field_name: str, text: str) -> None:
        customer, passenger = self.d.customer, self.d.passenger
        super()._answer_loose(field_name, text)
        self._customer_picked(customer, passenger)

    def _customer_picked(self, old: Customer | None, old_passenger: Passenger | None) -> None:
        """After a new customer is set: keep the booking's travel agent where the API needs it, and
        ask whether the passenger changes too."""
        customer = self.d.customer
        if customer is None or customer is old:
            return  # not picked yet (an agent's client list, the next new-customer question)
        if customer.travel_client_acc == "travel_loose_customer":
            if self.mode == "travel_agent":
                customer.acc_id = old.acc_id if old is not None else ""
            elif self._picked_agent is not None:
                customer.acc_id = self._picked_agent.id  # the admin form sends the agent with a new client
        elif self.mode == "travel_agent" and old is not None:
            customer.acc_id = old.acc_id
        offered, self.d.passenger = self.d.passenger, old_passenger
        if (
            offered is None
            or not offered.name
            or (
                old_passenger is not None
                and (offered.name, offered.cell) == (old_passenger.name, old_passenger.cell)
            )
        ):
            return
        self._new_passenger = offered
        keep = (
            old_passenger.name
            if old_passenger is not None and old_passenger.name
            else "the current passenger"
        )
        self._ask(
            "passenger_choice",
            f"Make {offered.name} the passenger too?  1. Yes, {offered.name}  2. No, keep {keep}",
        )

    # --- answers ------------------------------------------------------------------------

    async def _answer(self, key: str, text: str) -> None:
        if key in ("passenger_choice", "card_result"):
            self.pending = None
        if key == "passenger_choice":
            choice = text.strip().lower()
            answer = True if choice == "1" else False if choice == "2" else parse_yes_no(text)
            if answer is None:
                self._repeat(key, "  Please answer 1 (the new customer) or 2 (keep the passenger).")
                return
            if answer and self._new_passenger is not None:
                self.d.passenger = self._new_passenger
            self._new_passenger = None
            return
        if key == "card_result":
            await self._card_result(text)
            return
        if key == "proceed":
            answer = parse_yes_no(text)
            self.pending = None
            if answer is None:
                self._repeat(key, "  Please answer yes or no.")
            elif answer:
                self._proceed = False
                await self._submit()
                self._stall()
            else:
                self.say("  Not saved. Change something else, 'undo', or 'cancel'.")
                self._stall()
            return
        await super()._answer(key, text)

    async def _answer_confirm(self, text: str) -> None:
        answer = parse_yes_no(text)
        if answer is None:
            self._repeat("confirm", "  Please answer yes to save the changes, or no.")
            return
        self._awaiting_confirm = False
        if not answer:
            self.say("  Not saved. Change something else, 'undo', or 'cancel'.")
            self._stall()
            return
        await self._submit()
        self._stall()

    async def _back(self) -> None:
        if self.menu is not None and self.menu.kind in ("vehicle", "category"):
            await super()._back()
            return
        self.say("  Say 'undo' to revert the last change, or 'cancel' to stop.")
        self._stall()

    def _set_service(self, service: str, hours: int | None) -> None:
        if service == "round_trip":
            self.say(_ROUND_TRIP_REFUSAL)
            return
        super()._set_service(service, hours)

    # --- the main loop ------------------------------------------------------------------

    async def _advance(self) -> None:
        if self.reservation_id is None or self.finished:
            return None
        while self._place_queue:
            slot, text = self._place_queue.pop(0)
            if not await self._resolve(slot, text):
                return None
        if self.pending is not None or self.menu is not None:
            return None
        if not self._changes() and not self._vehicle_changed():
            self.say("  Nothing has changed yet.")
            self._prompt_for_change()
            return self._stall()
        return await super()._advance()

    def _invalidate_trip(self) -> None:
        """A trip detail changed: the price may change, the booked vehicle stays."""
        self.d.rates = self.d.return_rates = None

    async def _routes(self) -> bool:
        legs = self.d.legs
        for index, leg in enumerate(legs):
            if self._route_keys.get(index) != _route_key(leg):
                leg.route = None
        if not await super()._routes():
            return False
        self._route_keys = {index: _route_key(leg) for index, leg in enumerate(legs)}
        return True

    def _form(self) -> dict[str, Any]:
        """The loaded booking with only the fields the changes touched replaced."""
        categories = (self._common or {}).get("vehicleCategories")
        before = form_value(self._before, mode=self.mode, vehicle_categories=categories)
        after = form_value(self.d, mode=self.mode, vehicle_categories=categories)
        v = copy.deepcopy(self._original)
        vehicle_changed, customer_changed = self._vehicle_changed(), self._customer_changed()
        for key, value in after.items():
            if key in _CUSTOMER_KEYS:
                if customer_changed:
                    v[key] = value
                continue
            # a new vehicle brings its own operator, vehicle and driver: none of the old ones stay
            if before.get(key) != value or (vehicle_changed and key.startswith(_VEHICLE_KEY_PREFIXES)):
                v[key] = value
        # driver instructions: the saved text stays, a note is added after it (a new pickup kind
        # starts again from that kind's default text, as the web form does)
        kind = self.d.outbound.pickup.kind if self.d.outbound.pickup else "city"
        old_kind = self._before.outbound.pickup.kind if self._before.outbound.pickup else "city"
        text = self._original["booking_instructions"] if kind == old_kind else default_instructions(kind)
        if self.d.instructions:
            text += f"<p>{html.escape(self.d.instructions)}</p>"
        v["booking_instructions"] = text
        return v

    async def _load_rates(self) -> bool:
        if not self._reprice_needed():
            self.d.rates = self.d.return_rates = None
            return True
        return await super()._load_rates()

    def _rates_failed(self) -> bool:
        if self._vehicle_changed():
            return super()._rates_failed()
        self.d.rates = self.d.return_rates = None
        self.say("  Say 'vehicles' to pick another vehicle, 'undo' to go back, or 'cancel'.")
        return False

    def _reprice_needed(self) -> bool:
        if not self.is_admin or not self._keep_price or self._vehicle_changed():
            return True
        # "Prevent System Rate Override" still refetches when the service type changes
        return self.d.service_type != self._before.service_type

    def _flags(self) -> ShareFlags:
        """As for a new booking, but an admin edit takes "created by admin" from the booking
        (``isCreatedByAdmin = created_by == 1``, booking.ts:3545), not from who edits it."""
        flags = super()._flags()
        if not self.is_admin:
            return flags
        return ShareFlags(
            is_travel_share=flags.is_travel_share,
            is_created_by_admin=str(self._data.get("created_by")) == "1",
        )

    def _customer_changed(self) -> bool:
        return _customer_key(self.d.customer) != _customer_key(self._before.customer)

    def _vehicle_changed(self) -> bool:
        return _vehicle_key(self.d.vehicle) != _vehicle_key(self._before.vehicle)

    # --- summary and save ---------------------------------------------------------------

    def _payment_note(self) -> str | None:
        return None

    def _confirm_prompt(self) -> str:
        return f"Save these changes to booking #{self.reservation_id}? (yes/no)"

    def _show_summary(self, *, final: bool) -> None:
        if final:
            self._show_changes()
            return
        super()._show_summary(final=False)
        saved = self._saved_total()
        if self.d.rates is None and saved is not None:
            self.say(f"  Price:      {_money(self.d.currency_symbol, saved)} (saved)")
        instructions = _plain(str(self._original.get("booking_instructions") or ""))
        if instructions:
            self.say(f"  Driver instructions: {instructions}")

    def _show_changes(self) -> None:
        d, sym = self.d, self.d.currency_symbol
        self.say(f"  ---- Changes to booking #{self.reservation_id} ----")
        for label, old, new in self._changes():
            self.say(f"  {label}: {old} → {new}")
        if self._vehicle_changed():
            self.say(f"  Vehicle: {_vehicle_text(self._before.vehicle)} → {_vehicle_text(d.vehicle)}")
        saved = self._saved_total()
        if d.rates is not None:
            new = d.rates.grand_total
            if saved is not None and abs(saved - new) >= 0.01:
                self.say(f"  Price: {_money(sym, saved)} → {_money(sym, new)}")
            elif saved is not None:
                self.say(f"  Price: {_money(sym, new)} (unchanged)")
            else:
                self.say(f"  Price: {_money(sym, new)} (worked out again for the changed booking)")
        elif saved is not None:
            self.say(f"  Price: {_money(sym, saved)} (kept; say 'reprice' for the system price)")
        operator = (d.vehicle or {}).get("affiliate_id")
        if self._vehicle_changed() and operator != (self._before.vehicle or {}).get("affiliate_id"):
            self.say(
                "  Note: the new operator is asked to accept the ride, so the booking goes back to pending."
            )
        seats = _int((d.vehicle or {}).get("passenger"))
        if seats and d.pax and d.pax > seats:
            self.say(f"  Note: the vehicle takes {seats} passengers; say 'vehicles' to pick a bigger one.")
        for warning in self._related_warnings():
            self.say(f"  Warning: {warning}")
        if not self.is_admin and d.outbound.date and d.outbound.time:
            at = datetime.combine(
                d.outbound.date, datetime.strptime(d.outbound.time, "%H:%M:%S").time(), self.now().tzinfo
            )
            if at - self.now() <= timedelta(hours=24):
                self.say("  Note: this pickup is within 24 hours.")

    def _changes(self) -> list[tuple[str, str, str]]:
        """(what, old, new) for each detail that differs from the loaded booking."""
        a, b = self._before, self.d
        rows: list[tuple[str, str, str]] = []

        def add(label: str, old: str, new: str) -> None:
            if old != new:
                rows.append((label, old or "none", new or "none"))

        if self._customer_changed():
            add("Customer", a.customer.label if a.customer else "", b.customer.label if b.customer else "")
        add("Service", _service_text(a), _service_text(b))
        add("Pickup", _when(a.outbound), _when(b.outbound))
        add("From", _place_text(a.outbound.pickup), _place_text(b.outbound.pickup))
        add(
            "Stops",
            "; ".join(s.short for s in a.outbound.stops),
            "; ".join(s.short for s in b.outbound.stops),
        )
        add("To", _place_text(a.outbound.dropoff), _place_text(b.outbound.dropoff))
        add("Passengers", str(a.pax), str(b.pax))
        add("Bags", str(a.luggage), str(b.luggage))
        add("Arriving flight", _arriving(a.outbound), _arriving(b.outbound))
        add("Departing flight", _departing(a.outbound), _departing(b.outbound))
        add("Cruise", _cruise(a.outbound), _cruise(b.outbound))
        add("Meet & greet", _greet(a.outbound), _greet(b.outbound))
        pa, pb = a.passenger or Passenger(), b.passenger or Passenger()
        add("Passenger", pa.name, pb.name)
        add("Passenger phone", _phone_text(pa), _phone_text(pb))
        add("Passenger email", pa.email, pb.email)
        add("Added note", a.instructions, b.instructions)
        return rows

    def _related_warnings(self) -> list[str]:
        d = self.d
        if d.outbound.date is None or d.outbound.time is None:
            return []
        new_at = datetime.combine(d.outbound.date, datetime.strptime(d.outbound.time, "%H:%M:%S").time())
        warnings = []
        for leg in self._related:
            if leg.pickup_at is None:
                continue
            when = _at_text(leg.pickup_at)
            if leg.is_outbound and new_at <= leg.pickup_at:
                warnings.append(
                    f"this is the return of booking #{leg.id} ({when}); the new pickup is before it."
                )
            elif not leg.is_outbound and new_at >= leg.pickup_at:
                warnings.append(f"the return ride, booking #{leg.id}, is on {when}, before the new pickup.")
        return warnings

    def _saved_total(self) -> float | None:
        return _float((self._saved_rates or {}).get("grand_total"))

    async def _submit(self) -> None:
        logger.info("edit_submit_called", reservation_id=self.reservation_id)  # TEMP: duplicate-edit trace
        d = self.d
        await self._common_data()
        v = self._form()
        finish_form(v, round_trip=False)
        v["service_type"] = "charter_tour" if d.is_charter else "one_way"  # never round_trip (a new leg)
        v["reservation_id"] = self.reservation_id
        v["updateType"] = "edit"
        v["proceed"] = self._proceed
        v["platform_type"] = "web"
        v["currency"] = booking_currency(d) or self._data.get("currency") or "usd"
        v["is_master_vehicle"] = d.is_master_vehicle if self._vehicle_changed() else False
        v["is_pending_affiliate"] = v["affiliate_type"] == "in_progress_affiliate"
        v["return_is_pending_affiliate"] = v["return_affiliate_type"] == "in_progress_affiliate"
        if self.mode != "individual":
            v["booking_created_from"] = self._flags().booking_created_from
        try:
            if d.rates is not None:
                apply_rates(v, d, self._flags())
                v["prevent_rate_override"] = False
            elif self._saved_rates is not None:
                saved = self._saved_rates
                v["rateArray"] = saved["rateArray"]
                v["sub_total"], v["grand_total"] = saved.get("sub_total"), saved.get("grand_total")
                v["min_rate_involved"] = bool(saved.get("min_rate_involved"))
                v["shares_array"] = _json_dict(self._data.get("share_array"))
                if self._customer_changed():
                    # the split follows the customer (travel agent or not); the saved total stays
                    v["shares_array"] = self._shares_on_saved_price(v)
            else:
                raise ValueError("the booking has no price to save")
        except (ValueError, RatesError) as exc:
            self.say(f"  Can't save the changes: {exc}. Nothing was sent.")
            return
        path = self.paths.edit_reservation
        customer = d.customer
        if customer is not None and customer.loose is not None:
            # A new customer's card goes with this request: the chat widget adds it and sends it.
            holder = " ".join(customer.loose.get(k, "") for k in ("first_name", "last_name")).strip()
            self._card_request = {
                "method": self.paths.edit_method,
                "path": path,
                "payload": v,
                "card_holder": holder,
            }
            self.card_request = self._card_request
            # TEMP: duplicate-edit trace; the widget sends the edit, the agent does not
            logger.info("edit_card_request_handed_to_widget", reservation_id=self.reservation_id, path=path)
            self._ask(
                "card_result",
                f"Enter {holder or 'the new customer'}'s card in the form below to save the changes. "
                "The card goes straight to 1800limo; I never see it.",
            )
            return
        self.say("  Saving...")
        logger.info("edit_sent_by_agent", reservation_id=self.reservation_id, path=path)  # TEMP: duplicate-edit trace
        try:
            if self.paths.edit_method == "PUT":
                answer = await self.api.put(path, v)
            else:
                answer = await self.api.post(path, v)
        except ApiUnreachableError as exc:
            self.say(
                f"  {exc}. I can't tell whether booking #{self.reservation_id} changed, so I won't resend."
            )
            self.say("  Check the booking first. To try again anyway, say 'save' and confirm.")
            return
        except ApiError as exc:
            self.say(f"  Booking #{self.reservation_id} was not changed: {exc.plain()}")
            self.say("  Fix it (or 'undo') and confirm again; nothing is resent on its own.")
            return
        data = answer.get("data") or {}
        if isinstance(data, dict) and data.get("is_confirm") is False:
            self._ask("proceed", f"{answer.get('message') or 'Please confirm.'} Save anyway? (yes/no)")
            return
        self._saved = True
        self.say(f"  Booking #{self.reservation_id} updated.")
        self.finished = True

    def _shares_on_saved_price(self, v: dict[str, Any]) -> dict[str, Any]:
        """``shares_array`` for the new customer, worked out on the saved rate lines."""
        saved = self._saved_rates or {}
        flags = self._flags()
        hours = float(self.d.hours if self.d.is_charter else 2)
        rates = outbound_rates(
            saved.get("rateArray") or {},
            service_type=v["service_type"],
            hours=hours,
            min_rate_involved=bool(saved.get("min_rate_involved")),
            flags=flags,
        )
        return share_array(
            rates,
            service_type=v["service_type"],
            hours=float(v["number_of_hours"]),
            account_type=v["account_type"],
            flags=flags,
            vehicles=int(v["number_of_vehicles"] or 0),
        )

    async def _card_result(self, text: str) -> None:
        """What the chat widget reports after sending the card with the edit (``card:ok`` /
        ``card:error <message>``). An "ok" is checked against the booking itself."""
        outcome, _, message = text.strip().partition(" ")
        logger.info("edit_card_result", reservation_id=self.reservation_id, outcome=outcome)  # TEMP: duplicate-edit trace
        if outcome == "card:error":
            reason = message.strip() or "the card was refused"
            self.say(f"  Booking #{self.reservation_id} was not changed: {reason}.")
            self.card_request = self._card_request  # the form is shown again
            self._ask(
                "card_result", "Check the details and try again in the form, or say 'undo' or 'cancel'."
            )
            return
        if outcome != "card:ok":
            self.card_request = self._card_request
            self._repeat(
                "card_result", "  Please enter the card in the form below, or say 'undo' or 'cancel'."
            )
            return
        try:
            answer = await self.api.get(self.paths.get_reservation.format(id=self.reservation_id))
            data = answer.get("data") or {}
        except ApiError:
            data = {}
        before = (str(self._data.get("acc_id") or ""), str(self._data.get("travel_client_id") or ""))
        now = (str(data.get("acc_id") or ""), str(data.get("travel_client_id") or ""))
        if data and now != before:
            self._saved = True
            self.card_request = None
            self.say(f"  Booking #{self.reservation_id} updated, with the new customer and their card.")
            self.finished = True
            return
        self.say(
            f"  I couldn't confirm that booking #{self.reservation_id} changed. Check it before trying again."
        )
        self._stall()


def _customer_key(customer: Customer | None) -> tuple[Any, ...] | None:
    if customer is None:
        return None
    loose = tuple(sorted((customer.loose or {}).items()))
    return (
        customer.account_type,
        str(customer.acc_id),
        customer.travel_client_acc,
        str(customer.travel_client_id),
        loose,
    )


def _route_key(leg: Leg) -> tuple[Any, ...]:
    return (leg.pickup, tuple(leg.stops), leg.dropoff)


def _vehicle_key(vehicle: dict[str, Any] | None) -> tuple[Any, ...] | None:
    if vehicle is None:
        return None
    return (
        str(vehicle.get("id") or ""),
        str(vehicle.get("affiliate_id") or ""),
        bool(vehicle.get("is_master_vehicle")),
    )


def _vehicle_text(vehicle: dict[str, Any] | None) -> str:
    if not vehicle:
        return "none"
    details = vehicle.get("vehicle_details") or {}
    title = " ".join(str(x) for x in (details.get("year"), details.get("make"), details.get("model")) if x)
    name = str(vehicle.get("name") or "")
    return f"{title} ({name})" if title and name else title or name or "vehicle"


def _service_text(d: BookingDraft) -> str:
    text = _SERVICE_NAMES.get(d.service_type or "", "-")
    return f"{text}, {d.hours} hours" if d.is_charter else text


def _when(leg: Leg) -> str:
    return f"{_day(leg.date)} {_clock(leg.time)}" if leg.date and leg.time else ""


def _place_text(place: Place | None) -> str:
    return place.short if place else ""


def _arriving(leg: Leg) -> str:
    flight = leg.pickup_flight
    if not leg.pickup or leg.pickup.kind != "airport" or not flight.airline:
        return ""
    return f"{airline_label(flight.airline)} {flight.number} from {flight.city}".strip()


def _departing(leg: Leg) -> str:
    flight = leg.dropoff_flight
    if not leg.dropoff or leg.dropoff.kind != "airport" or not flight.airline:
        return ""
    return f"{airline_label(flight.airline)} {flight.number}".strip()


def _cruise(leg: Leg) -> str:
    if not leg.cruise.port and not leg.cruise.name:
        return ""
    return f"{leg.cruise.name} at {leg.cruise.port} ({leg.cruise.time})"


def _greet(leg: Leg) -> str:
    return str((leg.meet_greet or {}).get("message") or "")


def _phone_text(passenger: Passenger) -> str:
    return f"{passenger.cell_isd} {passenger.cell}" if passenger.cell else ""


def _at_text(at: datetime | None) -> str:
    return f"{_day(at.date())} {_clock(at.strftime('%H:%M:%S'))}" if at else "-"


def _short(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _status_words(status: str) -> str:
    return {
        "pending": "awaiting confirmation",
        "assigned": "confirmed",
        "accepted": "confirmed",
        "driver_unassigned": "needs a driver",
        "rejected": "rejected by the operator",
    }.get(status, status.replace("_", " ") or "unknown status")


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}
