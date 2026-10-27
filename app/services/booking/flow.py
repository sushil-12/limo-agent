"""The "book a ride" conversation: collect the trip, pick a vehicle, confirm, then submit once.

Order and rules follow BOOKING_FLOW.md. Only what is missing is asked. A booking is
submitted only after the full summary and an explicit "yes", and never retried automatically.

At any point the user can say:
    change passengers to 3 | change date to 15 nov | change pickup to ORD | ...
    back          the previous menu (vehicles -> categories -> trip details)
    vehicles      show the vehicle list again
    summary       show what is collected so far
    cancel        stop without booking
A bare number picks from the latest numbered menu.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.services.booking.api import ROLE_PATHS, ApiError, ApiUnreachableError, LimoApi
from app.services.booking.card_form import CardResult, collect_card
from app.services.booking.draft import BookingDraft, Customer, Leg, Passenger, Place
from app.services.booking.maps import GoogleMaps, MapsError, PlaceDetails
from app.services.booking.parse import (
    ParsedTrip,
    TimeOfDay,
    parse_count,
    parse_date,
    parse_service,
    parse_time,
    parse_trip,
    parse_yes_no,
)
from app.services.booking.payload import (
    MEET_GREET_AIRPORT,
    MEET_GREET_CITY,
    airline_label,
    booking_data,
    default_instructions,
    form_value,
    listing_payload,
    quote_payload,
    submit_payload,
)
from app.services.booking.rates import (
    RatesError,
    ShareFlags,
    outbound_rates,
    return_rate_array,
    return_rates,
)
from app.services.booking.reference import Airport, AirportIndex

Say = Callable[[str], None]
CardCollector = Callable[..., Awaitable[CardResult]]
AccountLookup = Callable[[int], Awaitable[int | None]]  # reservation id -> tbl_reservations.acc_id

_MENU_LIMIT = 10
_EMAIL = re.compile(r"^[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
_ISD_COUNTRIES = {
    "+1": "us", "+44": "gb", "+91": "in", "+61": "au", "+971": "ae", "+65": "sg", "+49": "de", "+33": "fr",
    "+81": "jp", "+86": "cn", "+52": "mx", "+34": "es", "+39": "it", "+353": "ie", "+64": "nz", "+27": "za",
}  # fmt: skip
_SERVICE_NAMES = {"one_way": "one way", "round_trip": "round trip", "charter_tour": "charter"}
_CHANGE = re.compile(
    r"^(?:change|set|update|make)\s+(?:the\s+)?(?P<field>.+?)\s+(?:to|as|=)\s+(?P<value>.+)$", re.I
)
_ADD_STOP = re.compile(r"^add\s+(?:a\s+)?(?P<ret>return\s+)?stop\s+(?:at\s+)?(?P<value>.+)$", re.I)
_AIRPORT_WORDS = re.compile(r"\b(?:international\s+)?(?:airport|airpot|aiport|intl)\b|\bka\s+airport\b", re.I)
_CODE = re.compile(r"^\s*([A-Za-z]{3})(?:\s+(?:airport|intl))?\s*$")


@dataclass
class Menu:
    kind: str
    options: list[Any]


@dataclass
class _Account:
    id: Any
    label: str


@dataclass
class BookingFlow:
    mode: str  # admin | individual | travel_agent
    user_id: int
    api: LimoApi
    maps: GoogleMaps
    airports: AirportIndex
    say: Say = print
    now: Callable[[], datetime] = field(default=lambda: datetime.now().astimezone())
    collect_card: CardCollector = collect_card
    account_for_reservation: AccountLookup | None = None

    d: BookingDraft = field(default_factory=BookingDraft)
    pending: str | None = None
    menu: Menu | None = None
    finished: bool = False
    _place_queue: list[tuple[str, str]] = field(default_factory=list)
    _type_ids: dict[str, int] | None = None
    _common: dict[str, Any] | None = None
    _categories: list[dict[str, Any]] = field(default_factory=list)
    _vehicles: list[dict[str, Any]] = field(default_factory=list)
    _last_category: str | None = None
    _accounts: dict[str, list[_Account]] = field(default_factory=dict)
    _picked_agent: _Account | None = None
    _loose: dict[str, str] = field(default_factory=dict)
    _hours_confirmed: bool = False
    _relative_checked: bool = True
    _relative_word: str | None = None
    _detail_skips: set[str] = field(default_factory=set)
    _meet_greet_done: set[str] = field(default_factory=set)
    _awaiting_confirm: bool = False
    _booked: dict[str, Any] | None = None
    _stalled: bool = False

    @property
    def paths(self):  # noqa: ANN201
        return ROLE_PATHS[self.mode]

    @property
    def is_admin(self) -> bool:
        return self.mode == "admin"

    # --- entry points ------------------------------------------------------------

    async def start(self, text: str = "") -> None:
        self.say("Booking a ride. Say 'cancel' at any time to stop; nothing is booked until you confirm.")
        if text.strip():
            await self._merge(parse_trip(text, self._today()))
        await self._advance()

    async def handle(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        self._stalled = False
        if await self._command(text):
            pass
        elif self.menu is not None and text.isdigit():
            await self._pick(int(text))
        elif self.pending is not None:
            await self._answer(self.pending, text)
        elif self.menu is not None and self.menu.kind.startswith("place:"):
            slot = self.menu.kind.split(":", 1)[1]
            self.menu = None
            self._place_queue.insert(0, (slot, text))
        else:
            parsed = parse_trip(text, self._today())
            if not await self._merge(parsed):
                options = f"1-{len(self.menu.options)}" if self.menu else "an answer"
                self.say(
                    f"  Sorry, I didn't get that. Type {options}, 'change ... to ...', 'back' or 'cancel'."
                )
                return
        if (
            not self.finished
            and not self._stalled
            and self.pending is None
            and (self.menu is None or self._place_queue)
        ):
            await self._advance()

    def _stall(self) -> None:
        """Something failed and was explained; wait for the user instead of calling the API again."""
        if self.pending is None and self.menu is None:
            self._stalled = True

    # --- commands ----------------------------------------------------------------

    async def _command(self, text: str) -> bool:
        lowered = text.lower().strip(" .!")
        if lowered in ("cancel", "stop", "quit", "exit", "cancel booking", "rehne do", "chhodo"):
            self.finished = True
            self.say("  Cancelled. Nothing was booked." if self._booked is None else "  Done.")
            return True
        if lowered in ("back", "go back", "peeche", "previous"):
            await self._back()
            return True
        if lowered in ("summary", "show summary", "status"):
            if self.pending is not None or self.menu is not None or self.finished:
                self._show_summary(final=False)
            return True  # otherwise the next step shows it (with the confirm question when ready)
        if lowered in ("vehicles", "show vehicles", "other vehicles", "show other vehicles", "categories"):
            await self._back_to_vehicles(categories=lowered == "categories")
            return True
        if lowered in ("help", "?"):
            self.say(__doc__.split("At any point", 1)[1].strip() if __doc__ else "")
            return True
        if match := _ADD_STOP.match(text):
            slot = "return_stop" if match.group("ret") else "stop"
            self._place_queue.append((slot, match.group("value")))
            self._invalidate_trip()
            self.pending = self.menu = None
            return True
        if match := _CHANGE.match(text):
            await self._change(match.group("field").lower().strip(), match.group("value").strip())
            return True
        return False

    async def _change(self, name: str, value: str) -> None:
        d, today = self.d, self._today()
        self.pending = self.menu = None
        self._awaiting_confirm = False
        trip = True

        def says(pattern: str) -> bool:
            return re.search(rf"\b(?:{pattern})\b", name) is not None

        if says(r"airline|flight|origin city|coming from|meet|greet|meet and greet|cruise|ship|port"):
            self._reset_details(name)
            trip = False
        elif says(r"customer|account|client"):
            d.customer = d.passenger = None
            self._loose, self._picked_agent = {}, None
            trip = False
        elif says(r"notes?|instructions?"):
            d.instructions = value
            trip = False
        elif says(r"(?:passenger|pax) name|name"):
            d.passenger = d.passenger or Passenger()
            d.passenger.name = value
            trip = False
        elif says(r"(?:passenger|pax) (?:phone|cell|mobile|number)|phone|mobile|cell"):
            d.passenger = d.passenger or Passenger()
            if not self._set_passenger_phone(value):
                self.say("  That phone number doesn't look right (4-15 digits, optional +country code).")
            trip = False
        elif says(r"(?:passenger )?email"):
            d.passenger = d.passenger or Passenger()
            if _EMAIL.match(value):
                d.passenger.email = value
            else:
                self.say("  That email doesn't look right.")
            trip = False
        elif says(r"passengers?|pax|people|log|persons?"):
            count = parse_count(value)
            if count is None or count < 1:
                self.say("  Passengers must be a number of 1 or more.")
                return
            d.pax = count
        elif says(r"luggage|bags?|saman|suitcases?"):
            count = parse_count(value)
            if count is None:
                self.say("  Luggage must be a number (0 for none).")
                return
            d.luggage = count
        elif says(r"return (?:date|day)"):
            found = parse_date(value, today)
            if found is None:
                self.say("  I couldn't read that date.")
                return
            d.ret.date = found[0]
        elif says(r"return (?:pickup )?time"):
            if not self._set_time(d.ret, value):
                return
        elif says(r"return stops?"):
            d.ret.stops = []
        elif says(r"stops?"):
            d.outbound.stops = []
        elif says(r"(?:pickup )?(?:date|day)"):
            found = parse_date(value, today)
            if found is None:
                self.say("  I couldn't read that date.")
                return
            d.outbound.date = found[0]
        elif says(r"(?:pickup )?time"):
            if not self._set_time(d.outbound, value):
                return
        elif says(r"return (?:pick ?up|from)"):
            d.ret.pickup = None
            self._place_queue.append(("return_pickup", value))
        elif says(r"return (?:drop ?off|drop|to|destination)"):
            d.ret.dropoff = None
            self._place_queue.append(("return_dropoff", value))
        elif says(r"pick ?up|pick-up|from|origin|start"):
            d.outbound.pickup = None
            self._place_queue.append(("pickup", value))
        elif says(r"drop ?off|drop-off|drop|to|destination"):
            d.outbound.dropoff = None
            self._place_queue.append(("dropoff", value))
        elif says(r"hours?|ghante"):
            count = parse_count(value)
            if count is None:
                self.say("  Hours must be a number.")
                return
            d.hours, self._hours_confirmed = count, count >= 2
        elif says(r"service|trip type|trip|type"):
            service = parse_service(value)
            if service is None:
                self.say("  Say one way, round trip or charter.")
                return
            self._set_service(*service)
        else:
            self.say(
                f"  I can't change '{name}'. Try passengers, luggage, date, time, pickup, dropoff, "
                "service, hours, stops, return date/time, passenger name/phone, customer, notes."
            )
            return
        if trip:
            self._invalidate_trip()
        self.say("  Updated.")

    def _reset_details(self, name: str) -> None:
        for leg in self.d.legs:
            if "airline" in name:
                leg.pickup_flight.airline = leg.dropoff_flight.airline = None
            if "flight" in name:
                leg.pickup_flight.number = leg.dropoff_flight.number = ""
                leg.dropoff_flight.number_skipped = False
            if "origin" in name or "coming" in name:
                leg.pickup_flight.city = ""
            if "cruise" in name or "ship" in name or "port" in name:
                leg.cruise.port = leg.cruise.name = ""
        if "meet" in name or "greet" in name:
            self._meet_greet_done.clear()
        self._detail_skips.clear()

    def _invalidate_trip(self) -> None:
        """Trip details changed: drop routes and prices so the listings are called again."""
        if self.d.category is not None:
            self._last_category = self.d.category.get("name")
        self.d.clear_pricing()

    async def _back(self) -> None:
        self.pending = None
        self._awaiting_confirm = False
        on_menu = self.menu.kind if self.menu is not None else None
        if on_menu == "vehicle":
            await self._back_to_vehicles(categories=True)
        elif self.d.vehicle is not None:
            await self._back_to_vehicles(categories=self.d.is_master_vehicle)
        elif self.d.category is not None or on_menu == "category":
            self.d.category = None
            self._last_category = None
            self.menu = None
            self.say(
                "  What would you like to change? For example 'change date to 15 nov', "
                "'change passengers to 3' or 'change pickup to ORD'. ('vehicles' shows the list again.)"
            )
            self._stall()
        else:
            self.say("  Nothing to go back to yet. Answer the question, change a detail, or 'cancel'.")
            self._stall()

    async def _back_to_vehicles(self, *, categories: bool) -> None:
        self.pending = None
        self.d.vehicle = self.d.master_info = None
        self.d.rates = self.d.return_rates = None
        if categories or self.d.category is None or not self._vehicles:
            self.d.category = None
            self._last_category = None
            self.menu = None
            if self._categories:
                self._show_categories()
            return
        self._show_vehicles()

    # --- answers -----------------------------------------------------------------

    async def _answer(self, key: str, text: str) -> None:
        d = self.d
        self.pending = None
        if key in ("pickup", "dropoff", "return_pickup", "return_dropoff"):
            self._place_queue.append((key, text))
        elif key == "date":
            found = parse_date(text, self._today())
            if found is None:
                self._repeat(key, "  I couldn't read that date. For example: 14 nov, tomorrow, 2026-11-14.")
                return
            d.outbound.date, word = found
            self._note_relative(word, found[0])
        elif key in ("time", "return_time"):
            leg = d.outbound if key == "time" else d.ret
            if not self._set_time(leg, text):
                self.pending = key
        elif key in ("ampm", "return_ampm"):
            leg = d.outbound if key == "ampm" else d.ret
            choice = text.lower().strip(". ")
            if leg.time is None or choice not in (
                "am",
                "pm",
                "a",
                "p",
                "morning",
                "evening",
                "subah",
                "shaam",
            ):
                self._repeat(key, "  Please answer am or pm.")
                return
            hour, minute = int(leg.time[:2]), int(leg.time[3:5])
            pm = choice in ("pm", "p", "evening", "shaam")
            leg.time = TimeOfDay(hour % 12 + (12 if pm else 0), minute).value()
        elif key == "relative":
            answer = parse_yes_no(text)
            if answer is None:
                self._repeat(key, "  Please answer yes or no.")
                return
            self._relative_checked = True
            if not answer:
                d.outbound.date = None
        elif key == "service":
            service = parse_service(text)
            if service is None:
                self._repeat(key, "  Please answer 1 (one way), 2 (round trip) or 3 (charter).")
                return
            self._set_service(*service)
        elif key == "hours":
            count = parse_count(text)
            if count is None or count < 2:
                self._repeat(key, "  A charter is at least 2 hours. How many hours?")
                return
            d.hours, self._hours_confirmed = count, True
        elif key == "return_date":
            found = parse_date(text, self._today())
            if found is None:
                self._repeat(key, "  I couldn't read that date.")
                return
            d.ret.date = found[0]
        elif key == "return_route":
            answer = parse_yes_no(text)
            if answer is True:
                d.return_route_confirmed = True
            elif answer is False:
                d.ret.pickup = d.ret.dropoff = None
                d.return_route_confirmed = True
            else:
                parsed = parse_trip(text, self._today())
                if parsed.pickup or parsed.dropoff:
                    d.ret.pickup = d.ret.dropoff = None
                    if parsed.pickup:
                        self._place_queue.append(("return_pickup", parsed.pickup))
                    if parsed.dropoff:
                        self._place_queue.append(("return_dropoff", parsed.dropoff))
                    d.return_route_confirmed = True
                else:
                    self._repeat(key, "  Answer yes, or give the return trip as 'from X to Y'.")
                    return
        elif key in ("pax", "luggage"):
            both = parse_trip(text, self._today())  # "6 passengers 5 bags" answers both
            if both.pax is not None or both.luggage is not None:
                d.pax = both.pax if both.pax is not None else d.pax
                d.luggage = both.luggage if both.luggage is not None else d.luggage
                if d.pax is not None and d.pax >= 1:
                    return
            count = parse_count(text)
            if count is None or (key == "pax" and count < 1):
                self._repeat(key, "  Please give a number." + (" At least 1." if key == "pax" else ""))
                return
            if key == "pax":
                d.pax = count
            else:
                d.luggage = count
        elif key.startswith("airline_search:"):
            _, index, end = key.split(":")
            self._search_airline(int(index), end, text)
        elif key.startswith("meet_default:"):
            index = key.split(":")[1]
            if parse_yes_no(text) is True or text.lower().strip() in ("default", "ok", "skip"):
                self.menu = None
                self._meet_greet_done.add(f"meet:{index}")
            else:
                self._repeat(key, "  Pick a number from the list, or 'ok' for the default.")
        elif key in ("stops", "return_stops"):
            leg = d.outbound if key == "stops" else d.ret
            if parse_yes_no(text) is False or text.lower().strip() in (
                "none",
                "no stops",
                "0",
                "nahi",
                "done",
            ):
                leg.stops_answered = True
            elif parse_yes_no(text) is True:
                self._repeat(key, "  Type the stop's address.")
                return
            else:
                self._place_queue.append(("stop" if key == "stops" else "return_stop", text))
        elif key in ("customer_search", "agent_search", "client_search"):
            await self._search_accounts(key, text)
        elif key.startswith("loose:"):
            self._answer_loose(key.split(":", 1)[1], text)
        elif key == "passenger_name":
            d.passenger = d.passenger or Passenger()
            d.passenger.name = text
        elif key == "passenger_cell":
            d.passenger = d.passenger or Passenger()
            if not self._set_passenger_phone(text):
                self._repeat(
                    key, "  Please give a phone number (4-15 digits, optionally with +country code)."
                )
        elif key.startswith("detail:"):
            self._answer_detail(key.split(":", 1)[1], text)
        elif key == "confirm":
            await self._answer_confirm(text)
        elif key == "card":
            answer = parse_yes_no(text)
            if answer is None:
                self._repeat(key, "  Please answer yes or no.")
                return
            if answer:
                await self._add_card_for_new_customer()
            else:
                self.say("  OK, no card added. The customer can add one later.")
            self.finished = True

    def _repeat(self, key: str, message: str) -> None:
        self.say(message)
        self.pending = key

    def _set_time(self, leg: Leg, text: str) -> bool:
        found = parse_time(text)
        if found is None:
            self.say("  I couldn't read that time. For example: 5 am, 17:30, shaam 6 baje.")
            return False
        if found.minute % 15:
            self.say("  Pickup times go in 15-minute steps (:00, :15, :30, :45). Which time?")
            return False
        leg.time = found.value()
        if found.ambiguous:
            self.pending = "ampm" if leg is self.d.outbound else "return_ampm"
            self.say(f"  {found.hour}:{found.minute:02d} in the morning or evening? (am/pm)")
        return True

    def _set_service(self, service: str, hours: int | None) -> None:
        d = self.d
        d.service_type = service
        if service == "charter_tour":
            if hours is not None:
                d.hours, self._hours_confirmed = max(hours, 0), hours >= 2
        else:
            self._hours_confirmed = False
        if service != "round_trip":
            d.ret = Leg()
            d.return_route_confirmed = False

    def _note_relative(self, word: str | None, day: date) -> None:
        # "kal" means both yesterday and tomorrow, weekdays can mean this or next week: confirm those
        if word and word.lower() not in ("today", "aaj", "tonight"):
            self._relative_word = word
            self._relative_checked = False

    async def _merge(self, parsed: ParsedTrip) -> bool:
        """Fold what the user typed into the draft. Returns False if nothing was understood."""
        d = self.d
        changed = False
        if parsed.date is not None:
            d.outbound.date = parsed.date
            self._note_relative(parsed.relative_date, parsed.date)
            changed = True
        if parsed.time is not None:
            if parsed.time.minute % 15:
                self.say("  Pickup times go in 15-minute steps (:00, :15, :30, :45).")
            else:
                d.outbound.time = parsed.time.value()
                if parsed.time.ambiguous:
                    self.pending = "ampm"
                    self.say(
                        f"  {parsed.time.hour}:{parsed.time.minute:02d} in the morning or evening? (am/pm)"
                    )
            changed = True
        if parsed.service_type is not None:
            self._set_service(parsed.service_type, parsed.hours)
            changed = True
        elif parsed.hours is not None and d.is_charter:
            d.hours, self._hours_confirmed = parsed.hours, parsed.hours >= 2
            changed = True
        if parsed.pax is not None:
            d.pax, changed = parsed.pax, True
        if parsed.luggage is not None:
            d.luggage, changed = parsed.luggage, True
        if parsed.no_stops:
            d.outbound.stops_answered = d.ret.stops_answered = True
            changed = True
        if parsed.pickup:
            d.outbound.pickup = None
            self._place_queue.append(("pickup", parsed.pickup))
            changed = True
        if parsed.dropoff:
            d.outbound.dropoff = None
            self._place_queue.append(("dropoff", parsed.dropoff))
            changed = True
        if changed:
            self._invalidate_trip()
        return changed

    # --- the main loop: ask the next missing thing ---------------------------------

    async def _advance(self) -> None:
        d = self.d
        while self._place_queue:
            slot, text = self._place_queue.pop(0)
            if not await self._resolve(slot, text):
                return None
        if self.pending is not None or self.menu is not None:
            return None
        out = d.outbound
        if out.pickup is None:
            return self._ask(
                "pickup", "Where should the ride pick up? (an address, a place, or an airport code like ORD)"
            )
        if out.dropoff is None:
            hint = " (for a charter it can be the pickup again)" if d.is_charter else ""
            return self._ask("dropoff", f"Where to?{hint}")
        if out.date is None:
            return self._ask("date", "What date? (e.g. 14 nov, tomorrow)")
        if not self._relative_checked:
            self._relative_checked = True
            return self._ask("relative", f"'{self._relative_word}' = {_day(out.date)}. Right? (yes/no)")
        if out.time is None:
            return self._ask("time", "What pickup time? (e.g. 5 am, 17:30)")
        if d.service_type is None:
            return self._ask(
                "service", "One way, round trip, or charter (hourly)?  1. one way  2. round trip  3. charter"
            )
        if d.is_charter and not self._hours_confirmed:
            if d.hours < 2:
                self.say("  A charter is at least 2 hours.")
            return self._ask("hours", "How many hours for the charter? (minimum 2)")
        if d.is_round_trip:
            ret = d.ret
            if ret.date is None:
                return self._ask("return_date", f"Return date? (outbound is {_day(out.date)})")
            if ret.time is None:
                return self._ask("return_time", "Return pickup time?")
            if not d.return_route_confirmed:
                ret.pickup, ret.dropoff = ret.pickup or out.dropoff, ret.dropoff or out.pickup
                return self._ask(
                    "return_route",
                    f"Return trip: {ret.pickup.short} -> {ret.dropoff.short} (the reverse). OK? "
                    "(yes / or 'from X to Y')",
                )
            if ret.pickup is None:
                return self._ask("return_pickup", "Where should the return ride pick up?")
            if ret.dropoff is None:
                return self._ask("return_dropoff", "Where does the return ride go?")
        if d.pax is None:
            return self._ask("pax", "How many passengers?")
        if d.luggage is None:
            return self._ask("luggage", "How many bags?")
        # extra stops are optional, so they are not asked: 'add stop <address>' adds one
        if not self._check_trip():
            return None
        if not await self._routes():
            return self._stall()
        if d.category is None:
            await self._load_categories()
            return self._stall()
        if d.vehicle is None:
            return self._stall()
        if d.rates is None and not await self._load_rates():
            return self._stall()
        if d.customer is None:
            await self._ask_customer()
            if d.customer is None:
                return self._stall()
        if not await self._ask_passenger():
            return None
        await self._common_data()
        if not await self._ask_leg_details():
            return None
        self._show_summary(final=True)
        self._awaiting_confirm = True
        return self._ask("confirm", "Confirm booking? (yes/no)")

    def _ask(self, key: str, question: str) -> None:
        self.pending = key
        self.say(f"  {question}")

    def _check_trip(self) -> bool:
        d = self.d
        if len(d.outbound.stops) > 2 and not d.is_charter:
            d.service_type, d.ret = "charter_tour", Leg()
            d.hours, self._hours_confirmed = max(d.hours, 2), True
            self.say(
                "  More than 2 stops: the web app books this as a charter/tour, so it is now a charter "
                f"of {d.hours} hours. Say 'change hours to N' to adjust."
            )
        if not d.is_charter:
            for leg, label in ((d.outbound, "outbound"), (d.ret, "return")):
                if leg is d.ret and not d.is_round_trip:
                    continue
                points = [p for p in (leg.pickup, *leg.stops, leg.dropoff) if p is not None]
                if any(a.same_spot(b) for i, a in enumerate(points) for b in points[i + 1 :]):
                    self.say(
                        f"  The {label} pickup, drop-off and stops must all be different places "
                        "(the same place is only allowed for a charter)."
                    )
                    leg.dropoff = None
                    if leg is d.ret:
                        d.return_route_confirmed = True
                    self._ask(
                        "dropoff" if leg is d.outbound else "return_dropoff", "Where should it drop off?"
                    )
                    return False
        if not self.is_admin:
            now = self.now()
            for leg, key in ((d.outbound, "time"), (d.ret, "return_time")):
                if leg is d.ret and not d.is_round_trip:
                    continue
                if leg.date is None or leg.time is None:
                    continue
                at = datetime.combine(leg.date, datetime.strptime(leg.time, "%H:%M:%S").time(), now.tzinfo)
                if at < now:
                    self.say(f"  {_day(leg.date)} {_clock(leg.time)} is in the past.")
                    leg.time = None
                    if leg.date < now.date():
                        leg.date = None
                        self._ask("date" if key == "time" else "return_date", "Which date?")
                    else:
                        self._ask(key, "What pickup time?")
                    return False
        return True

    # --- places ------------------------------------------------------------------

    async def _resolve(self, slot: str, text: str) -> bool:
        """Resolve one typed place. False when a menu or question is now waiting."""
        if re.search(r"\bfbo\b|private\s+(?:jet|plane)", text, re.I):
            self.say(
                "  FBO / private-jet pickups aren't supported in the shell yet; book those in the web app."
            )
            return self._reask_place(slot)
        cruise = bool(re.search(r"\bcruise\b", text, re.I))
        code = _CODE.match(text)
        if code and not cruise and (airport := self.airports.by_code(code.group(1))):
            return self._set_place(slot, await self._airport_place(airport))
        city = _AIRPORT_WORDS.sub(" ", text).strip(" ,")
        if _AIRPORT_WORDS.search(text) and city and not cruise:
            matches = self.airports.in_city(city)
            if len(matches) == 1:
                return self._set_place(slot, await self._airport_place(matches[0]))
            if len(matches) > 1:
                self.menu = Menu(f"place:{slot}", [("airport", a) for a in matches[:_MENU_LIMIT]])
                self.say(
                    f"  {city.title()} has {len(matches)} airports. Which one for the {_slot_name(slot)}?"
                )
                for number, airport in enumerate(matches[:_MENU_LIMIT], start=1):
                    self.say(f"  {number}. {airport.code} - {airport.name}")
                return False
        try:
            suggestions = await self.maps.autocomplete(text)
        except MapsError as exc:
            self.say(f"  Couldn't look up '{text}': {exc}.")
            return self._reask_place(slot)
        if not suggestions:
            self.say(f"  I couldn't find '{text}'. Try a fuller address or a landmark name.")
            return self._reask_place(slot)
        if len(suggestions) > 1:
            self.menu = Menu(f"place:{slot}", [("google", s, cruise) for s in suggestions[:5]])
            self.say(f"  Which '{text}' for the {_slot_name(slot)}?")
            for number, suggestion in enumerate(suggestions[:5], start=1):
                self.say(f"  {number}. {suggestion.text}")
            self.say("  (pick a number, or type a more exact place)")
            return False
        return await self._set_google_place(slot, suggestions[0].place_id, cruise)

    async def _set_google_place(self, slot: str, place_id: str, cruise: bool) -> bool:
        try:
            details = await self.maps.details(place_id)
        except MapsError as exc:
            self.say(f"  Couldn't load that place: {exc}.")
            return self._reask_place(slot)
        return self._set_place(slot, self._place_from(details, cruise=cruise))

    def _place_from(self, details: PlaceDetails, *, cruise: bool = False) -> Place:
        if details.is_airport and not cruise:
            nearest = self.airports.nearest(details.lat, details.lng)
            return Place(
                kind="airport",
                address=details.airport_label,
                lat=details.lat,
                lng=details.lng,
                airport_id=nearest.id if nearest else "",
                airport_label=details.airport_label,
                country_names=details.country_names,
            )
        return Place(
            kind="cruise" if cruise else "city",
            address=details.airport_label,  # "<name> - <address>", as the web app labels a place
            lat=details.lat,
            lng=details.lng,
            country_names=details.country_names,
        )

    async def _airport_place(self, airport: Airport) -> Place:
        """An airport picked by code or city: Google's place for it when Google agrees on the internal
        airport, else the airports.json record."""
        try:
            for suggestion in (await self.maps.autocomplete(f"{airport.code} airport {airport.city}"))[:3]:
                details = await self.maps.details(suggestion.place_id)
                nearest = self.airports.nearest(details.lat, details.lng)
                if details.is_airport and nearest is not None and nearest.id == airport.id:
                    return self._place_from(details)
        except MapsError:
            pass
        return Place(
            kind="airport",
            address=airport.label,
            lat=airport.lat or 0.0,  # by_code / in_city only return airports with coordinates
            lng=airport.long or 0.0,
            airport_id=airport.id,
            airport_label=airport.label,
            country_names=(airport.country,),
        )

    def _set_place(self, slot: str, place: Place) -> bool:
        d = self.d
        if slot == "stop":
            d.outbound.stops.append(place)
            self.say(f"  Added stop: {place.short}. Another stop? (address, or 'no')")
            self.pending = "stops"
        elif slot == "return_stop":
            d.ret.stops.append(place)
            self.say(f"  Added return stop: {place.short}. Another? (address, or 'no')")
            self.pending = "return_stops"
        else:
            leg = d.ret if slot.startswith("return_") else d.outbound
            if slot.endswith("pickup"):
                leg.pickup = place
            else:
                leg.dropoff = place
            kind = {"airport": "airport", "cruise": "cruise port", "city": "address"}[place.kind]
            self.say(f"  {_slot_name(slot).capitalize()}: {place.short} ({kind})")
        self._invalidate_trip()
        return self.pending is None

    def _reask_place(self, slot: str) -> bool:
        if slot in ("stop", "return_stop"):
            self._ask("stops" if slot == "stop" else "return_stops", "Which stop address? (or 'no')")
        else:
            self._ask(slot, f"Where is the {_slot_name(slot)}?")
        return False

    # --- routes, categories, vehicles, rates -------------------------------------

    async def _routes(self) -> bool:
        for leg, label in ((self.d.outbound, "trip"), (self.d.ret, "return trip")):
            if leg is self.d.ret and not self.d.is_round_trip:
                continue
            if leg.route is not None or leg.pickup is None or leg.dropoff is None:
                continue
            points = [(p.lat, p.lng) for p in (leg.pickup, *leg.stops, leg.dropoff)]
            try:
                leg.route = await self.maps.route(points)
            except MapsError as exc:
                self.say(f"  Could not calculate the {label} route ({exc}). Check the pickup and drop-off.")
                return False
            if leg.route.meters <= 0:
                leg.route = None
                self.say(f"  The {label} has no driving distance. Use a different drop-off or add a stop.")
                return False
        return True

    async def _common_data(self) -> dict[str, Any]:
        """Airlines, meet & greet choices and vehicle categories (the web app's ``BigData``)."""
        if self._common is None:
            try:
                self._common = await self.api.common_data()
            except ApiError:
                self._common = {}
        return self._common

    def _form(self) -> dict[str, Any]:
        return form_value(
            self.d, mode=self.mode, vehicle_categories=(self._common or {}).get("vehicleCategories")
        )

    async def _load_categories(self) -> None:
        d = self.d
        quote = quote_payload(d, self._form())
        self.say("  Looking up vehicles...")
        try:
            answer = await self.api.master_vehicle_listing(quote)
        except ApiError as exc:
            self.say(f"  Couldn't load vehicles: {exc.plain()}")
            self.say("  Change a detail (e.g. 'change time to 6 am') or 'cancel'.")
            return
        d.currency_symbol = (answer.get("currency") or {}).get("symbol") or d.currency_symbol
        self._categories = list(answer.get("data") or [])
        if self._last_category:
            again = next((c for c in self._categories if c.get("name") == self._last_category), None)
            self._last_category = None
            if again is not None and self._fits(again):
                self.say(f"  Prices updated. Showing {again.get('name')} again ('categories' for all).")
                await self._choose_category(again)
                return
        self._show_categories()

    def _fits(self, item: dict[str, Any]) -> bool:
        pax, bags = _int(item.get("passenger")), _int(item.get("luggage"))
        return (pax is None or pax >= (self.d.pax or 1)) and (bags is None or bags >= (self.d.luggage or 0))

    def _show_categories(self) -> None:
        d = self.d
        # vehicle_count = 0 stays listed: the web app shows it with "Book Now" (the master vehicle)
        fitting = [c for c in self._categories if self._fits(c)]
        small = [c for c in self._categories if not self._fits(c)]
        if not fitting:
            self.menu = None
            note = " big enough" if small else ""
            self.say(f"  No vehicle categories{note} are available for this trip.")
            self.say("  Change something (date, time, passengers, places) or 'cancel'.")
            return
        self.menu = Menu("category", fitting)
        label = _SERVICE_NAMES[d.service_type or "one_way"]
        self.say(f"  Vehicle categories for {d.pax} passenger(s) / {d.luggage} bag(s):")
        for number, item in enumerate(fitting, start=1):
            price = (item.get(f"rate_breakdown_{d.service_type}") or {}).get("grand_total")
            count = item.get("vehicle_count")
            if count == 0:
                available_text = " — book now (no operator vehicles listed)"
            else:
                available_text = f" — {count} available" if count is not None else ""
            size = f"up to {item.get('passenger')} pax / {item.get('luggage')} bags"
            self.say(
                f"  {number}. {item.get('name')} — {size}{available_text} — "
                f"{_money(d.currency_symbol, price)} {label}"
            )
        if small:
            names = ", ".join(str(c.get("name")) for c in small)
            self.say(f"  Too small for {d.pax} passengers / {d.luggage} bags (hidden): {names}")
        self.say("  Pick a category number.")

    async def _choose_category(self, category: dict[str, Any]) -> None:
        d = self.d
        if category.get("vehicle_count") == 0:
            # master-vehicle bookNow: the category itself is booked as the master vehicle
            category = {**category, "is_master_vehicle": True}
        d.category = category
        if category.get("is_master_vehicle"):
            await self._choose_vehicle(category)
            return
        if self._type_ids is None:
            try:
                self._type_ids = await self.api.vehicle_type_ids()
            except ApiError:
                self._type_ids = {}
        type_id = self._type_ids.get(str(category.get("name")))
        if type_id is None:
            self.say(
                "  (No vehicle-type filter matches this category; the web app then lists every vehicle.)"
            )
        quote = quote_payload(d, self._form())
        try:
            answer = await self.api.vehicle_listing(listing_payload(quote, type_id, self.user_id))
        except ApiError as exc:
            self.say(f"  Couldn't load vehicles: {exc.plain()}")
            d.category = None
            self._show_categories()
            return
        d.currency_symbol = (answer.get("currency") or {}).get("symbol") or d.currency_symbol
        # select-vehicle's default Sort.LowToHigh on rate_breakdown_<service_type>.grand_total
        key = f"rate_breakdown_{d.service_type}"
        totals = [(_float((v.get(key) or {}).get("grand_total")), v) for v in answer.get("data") or []]
        totals.sort(key=lambda pair: (pair[0] is None, pair[0] or 0))
        self._vehicles = [v for _, v in totals]
        if not self._vehicles:
            self.say(f"  No vehicles are available for {category.get('name')}. Pick another category:")
            d.category = None
            self._show_categories()
            return
        self._show_vehicles()

    def _show_vehicles(self) -> None:
        d = self.d
        self.menu = Menu("vehicle", self._vehicles)
        self.say(f"  {d.category.get('name') if d.category else 'Vehicles'}:")
        for number, vehicle in enumerate(self._vehicles, start=1):
            details = vehicle.get("vehicle_details") or {}
            title = " ".join(
                str(x) for x in (details.get("year"), details.get("make"), details.get("model")) if x
            )
            line = f"  {number}. {title or vehicle.get('name')}"
            if title and vehicle.get("name"):
                line += f" ({vehicle.get('name')})"
            if self.is_admin:
                operator = vehicle.get("affiliate_company") or vehicle.get("affiliate_name")
                if operator:
                    phone = vehicle.get("affiliate_phone")
                    line += f" — {operator}" + (f" ({phone})" if phone else "")
            price = (vehicle.get(f"rate_breakdown_{d.service_type}") or {}).get("grand_total")
            line += f" — {vehicle.get('passenger')} pax / {vehicle.get('luggage')} bags"
            self.say(f"{line} — from {_money(d.currency_symbol, price)}")
        self.say("  Pick a vehicle number ('back' for categories).")

    async def _choose_vehicle(self, vehicle: dict[str, Any]) -> None:
        d = self.d
        d.vehicle = vehicle
        d.master_info = None
        self.menu = None
        if vehicle.get("is_master_vehicle") and self.is_admin:
            # loadMasterVehicleInfoForQuoteBot runs for the admin portal only (shouldBlockAdminApi)
            try:
                d.master_info = await self.api.master_vehicle_info(int(vehicle.get("id") or 0)) or None
            except (ApiError, ValueError):
                d.master_info = None
        await self._common_data()
        await self._load_rates()  # on failure it shows the vehicles again

    async def _load_rates(self) -> bool:
        d = self.d
        data = booking_data(d, self._form())
        try:
            answer = await self.api.booking_rates(data)
            flags = self._flags()
            hours = float(d.hours if d.is_charter else 2)
            d.rates = outbound_rates(
                answer.get("rateArray") or {},
                service_type=d.service_type or "one_way",
                hours=hours,
                min_rate_involved=bool(answer.get("min_rate_involved")),
                flags=flags,
            )
            d.return_rates = None
            if d.is_round_trip:
                d.return_rates = return_rates(
                    return_rate_array(answer), service_type="round_trip", hours=hours, flags=flags
                )
        except ApiError as exc:
            self.say(f"  Couldn't price that vehicle: {exc.plain()}")
            return self._rates_failed()
        except RatesError as exc:
            self.say(f"  Can't price that vehicle the way the web app does ({exc}).")
            return self._rates_failed()
        if d.rates.base_rate <= 0:
            self.say("  Base rate can not be empty: this vehicle has no rate for the trip.")
            return self._rates_failed()
        return True

    def _rates_failed(self) -> bool:
        self.d.vehicle = self.d.rates = self.d.return_rates = None
        if self._vehicles and self.d.category and not self.d.category.get("is_master_vehicle"):
            self.say("  Pick another vehicle:")
            self._show_vehicles()
        else:
            self.d.category = None
            self._show_categories()
        return False

    def _flags(self) -> ShareFlags:
        if self.mode == "travel_agent":
            return ShareFlags(is_travel_share=True, is_created_by_admin=False)
        if self.mode == "individual":
            return ShareFlags(is_travel_share=False, is_created_by_admin=False)
        travel = self.d.customer is not None and self.d.customer.account_type == "travel_planner"
        return ShareFlags(is_travel_share=travel, is_created_by_admin=True)

    # --- menus -------------------------------------------------------------------

    async def _pick(self, number: int) -> None:
        menu = self.menu
        if menu is None:
            return
        if not 1 <= number <= len(menu.options):
            self.say(f"  Pick a number from 1 to {len(menu.options)}.")
            return
        choice = menu.options[number - 1]
        self.menu = None
        self.pending = None  # a question shown with the menu (e.g. "or type more of the name")
        if menu.kind.startswith("place:"):
            slot = menu.kind.split(":", 1)[1]
            if choice[0] == "airport":
                self._set_place(slot, await self._airport_place(choice[1]))
            else:
                await self._set_google_place(slot, choice[1].place_id, choice[2])
        elif menu.kind == "category":
            await self._choose_category(choice)
        elif menu.kind == "vehicle":
            await self._choose_vehicle(choice)
        elif menu.kind == "customer_kind":
            await self._customer_kind(choice)
        elif menu.kind in ("account:customer", "account:agent", "account:client"):
            await self._pick_account(menu.kind.split(":", 1)[1], choice)
        elif menu.kind.startswith("airline:"):
            _, leg_index, end = menu.kind.split(":")
            leg = self.d.legs[int(leg_index)]
            (leg.pickup_flight if end == "pickup" else leg.dropoff_flight).airline = choice
        elif menu.kind.startswith("meet:"):
            leg = self.d.legs[int(menu.kind.split(":")[1])]
            leg.meet_greet = {"id": choice.get("id"), "message": choice.get("message")}
            self._meet_greet_done.add(menu.kind)

    # --- customer and passenger ----------------------------------------------------

    async def _ask_customer(self) -> None:
        if self.mode == "individual":
            await self._load_own_account()
            return
        if self.mode == "admin":
            options = [("individual", "An existing customer (search by name or phone)"),
                       ("travel_planner", "A travel agent's client"),
                       ("loose_customer", "A new customer")]  # fmt: skip
        else:
            options = [("travel_individual", "One of your existing clients"),
                       ("travel_loose_customer", "A new client")]  # fmt: skip
        self.menu = Menu("customer_kind", [key for key, _ in options])
        self.say("  Who is this booking for?")
        for number, (_, label) in enumerate(options, start=1):
            self.say(f"  {number}. {label}")

    async def _customer_kind(self, kind: str) -> None:
        if kind == "individual":
            self._ask("customer_search", "Customer name or phone number?")
        elif kind == "travel_planner":
            self._ask("agent_search", "Travel agent name, agency or phone?")
        elif kind == "travel_individual":
            self._ask("client_search", "Client name or phone number?")
        else:
            self._loose = {"_kind": kind}
            self._ask("loose:first_name", "New customer's first name?")

    async def _search_accounts(self, key: str, text: str) -> None:
        kind = {"customer_search": "customer", "agent_search": "agent", "client_search": "client"}[key]
        if text.lower() in ("new", "new customer", "new client"):
            await self._customer_kind("loose_customer" if self.is_admin else "travel_loose_customer")
            return
        path = {
            "customer": self.paths.accounts,
            "agent": "get-account-by-type/travel",
            "client": self.paths.accounts,
        }[kind]
        accounts = await self._account_list(kind, path)
        if accounts is None:
            return
        needle = text.lower().strip()
        digits = re.sub(r"\D", "", text)
        matches = [
            a for a in accounts
            if needle in a.label.lower() or (len(digits) >= 4 and digits in re.sub(r"\D", "", a.label))
        ]  # fmt: skip
        if not matches:
            self._repeat(
                key, f"  No {kind} matches '{text}'. Try again" + (", or 'new'." if kind != "agent" else ".")
            )
            return
        self._account_menu(kind, matches)

    def _account_menu(self, kind: str, matches: list[_Account]) -> None:
        shown = matches[:_MENU_LIMIT]
        self.menu = Menu(f"account:{kind}", shown)
        more = (
            f" (showing {len(shown)} of {len(matches)}; type more of the name to narrow)"
            if len(matches) > len(shown)
            else ""
        )
        self.say(f"  Which {kind}?{more}")
        for number, account in enumerate(shown, start=1):
            self.say(f"  {number}. {account.label}")
        if len(matches) > len(shown):
            self.pending = {
                "customer": "customer_search",
                "agent": "agent_search",
                "client": "client_search",
            }[kind]

    async def _account_list(self, kind: str, path: str) -> list[_Account] | None:
        if kind not in self._accounts:
            try:
                rows = (await self.api.get(path)).get("data") or []
            except ApiError as exc:
                self.say(f"  Couldn't load the {kind} list: {exc.plain()}")
                return None
            self._accounts[kind] = [_Account(r.get("id"), _account_label(r)) for r in rows if r.get("id")]
        return self._accounts[kind]

    async def _pick_account(self, kind: str, account: _Account) -> None:
        self.pending = None
        if kind == "agent":
            self._picked_agent = account
            try:
                rows = (await self.api.get(f"get-account-for-travel-agent/{account.id}")).get("data") or []
            except ApiError as exc:
                self.say(f"  Couldn't load that agent's clients: {exc.plain()}")
                return
            clients = [_Account(r.get("id"), _account_label(r)) for r in rows if r.get("id")]
            if not clients:
                self.say("  That travel agent has no clients yet. Pick another option:")
                await self._ask_customer()
                return
            self._accounts["client"] = clients
            if len(clients) <= _MENU_LIMIT:
                self._account_menu("client", clients)
            else:
                self._ask("client_search", f"{len(clients)} clients. Client name or phone?")
            return
        detail_path = (
            self.paths.account
            if (kind == "customer" or self.mode == "travel_agent")
            else ("get-an-account/{id}")
        )
        details = await self._account_details(detail_path.format(id=account.id))
        if details is None:
            return
        if kind == "customer":
            self.d.customer = Customer("individual", label=account.label, acc_id=account.id)
        elif self.mode == "admin":
            agent = self._picked_agent
            self.d.customer = Customer(
                "travel_planner",
                label=f"{account.label}, client of {agent.label if agent else 'travel agent'}",
                acc_id=agent.id if agent else "",
                travel_client_acc="travel_individual",
                travel_client_id=account.id,
            )
        else:
            self.d.customer = Customer(
                "travel_planner", label=account.label, travel_client_acc="travel_individual",
                travel_client_id=account.id,
            )  # fmt: skip
        self.d.passenger = _passenger_from(details)
        self.say(f"  Booking for {self.d.customer.label}.")

    async def _account_details(self, path: str) -> dict[str, Any] | None:
        try:
            return (await self.api.get(path)).get("data") or {}
        except ApiError as exc:
            self.say(f"  Couldn't load that account: {exc.plain()}")
            return None

    async def _load_own_account(self) -> None:
        details = await self._account_details(self.paths.account)
        if details is None:
            self.say("  Say 'summary' to retry, or 'cancel'.")
            return
        passenger = _passenger_from(details)
        self.d.customer = Customer("individual", label=passenger.name or "your account")
        self.d.passenger = passenger
        self.say(
            f"  Booking on your account. Passenger: {passenger.name or '-'}"
            f"{' (' + passenger.cell_isd + ' ' + passenger.cell + ')' if passenger.cell else ''}."
            " To book for someone else: 'change passenger name to ...'."
        )

    def _answer_loose(self, field_name: str, text: str) -> None:
        value = text.strip()
        steps = ["first_name", "last_name", "email", "phone"]
        if field_name == "email" and not _EMAIL.match(value):
            self._repeat(f"loose:{field_name}", "  That email doesn't look right. Email?")
            return
        if field_name == "phone":
            parsed = _phone(value)
            if parsed is None:
                self._repeat("loose:phone", "  Phone with country code, e.g. +1 312 555 0100?")
                return
            isd, number = parsed
            self._loose.update({"phone": number, "phone_isd": isd})
            country = _ISD_COUNTRIES.get(isd)
            if country is None:
                self._ask("loose:phone_country", f"Which country is {isd}? (2-letter code, e.g. in)")
                return
            self._loose["phone_country"] = country
        elif field_name == "phone_country":
            if not re.fullmatch(r"[A-Za-z]{2}", value):
                self._repeat("loose:phone_country", "  A 2-letter country code, please.")
                return
            self._loose["phone_country"] = value.lower()
        elif not value:
            self._repeat(f"loose:{field_name}", "  This is required.")
            return
        else:
            self._loose[field_name] = value
        nxt = next((s for s in steps if s not in self._loose), None)
        if nxt is not None:
            questions = {"last_name": "Last name?", "email": "Email?", "phone": "Phone (with country code)?"}
            self._ask(f"loose:{nxt}", questions[nxt])
            return
        loose = {k: v for k, v in self._loose.items() if not k.startswith("_")}
        name = f"{loose['first_name']} {loose['last_name']}"
        if self._loose["_kind"] == "loose_customer":
            self.d.customer = Customer("loose_customer", label=f"{name} (new customer)", loose=loose)
        else:
            self.d.customer = Customer(
                "travel_planner", label=f"{name} (new client)", travel_client_acc="travel_loose_customer",
                loose=loose,
            )  # fmt: skip
        # handleLooseCustomerName / handleLooseCustomerPhone copy the new customer onto the passenger
        self.d.passenger = Passenger(
            name=name, email=loose["email"], cell=loose["phone"], cell_isd=loose["phone_isd"],
            cell_country=loose["phone_country"],
        )  # fmt: skip

    async def _ask_passenger(self) -> bool:
        passenger = self.d.passenger
        if passenger is None or not passenger.name.strip():
            self._ask("passenger_name", "Passenger name?")
            return False
        if not re.fullmatch(r"[0-9+]{4,15}", passenger.cell or ""):
            self._ask("passenger_cell", f"Phone number for {passenger.name}?")
            return False
        return True

    def _set_passenger_phone(self, text: str) -> bool:
        parsed = _phone(text, default_isd=self.d.passenger.cell_isd if self.d.passenger else "+1")
        if parsed is None or self.d.passenger is None:
            return False
        isd, number = parsed
        self.d.passenger.cell, self.d.passenger.cell_isd = number, isd
        self.d.passenger.cell_country = _ISD_COUNTRIES.get(isd, self.d.passenger.cell_country)
        return True

    # --- flight / cruise / meet & greet ---------------------------------------------

    async def _ask_leg_details(self) -> bool:
        for index, leg in enumerate(self.d.legs):
            name = "return " if index else ""
            if leg.pickup is None or leg.dropoff is None:
                continue
            if leg.pickup.kind == "airport":
                if leg.pickup_flight.airline is None:
                    return await self._ask_airline(index, "pickup", f"Airline of the arriving {name}flight?")
                if not leg.pickup_flight.number:
                    self._ask(f"detail:{index}:pickup_flight", f"Arriving {name}flight number?")
                    return False
                if not leg.pickup_flight.city:
                    self._ask(f"detail:{index}:city", f"Which city is the {name}flight coming from?")
                    return False
                if f"meet:{index}" not in self._meet_greet_done:
                    return await self._ask_meet_greet(index)
            if leg.dropoff.kind == "airport":
                if leg.dropoff_flight.airline is None:
                    return await self._ask_airline(
                        index, "dropoff", f"Airline of the departing {name}flight?"
                    )
                if not leg.dropoff_flight.number and not leg.dropoff_flight.number_skipped:
                    self._ask(
                        f"detail:{index}:dropoff_flight", f"Departing {name}flight number? (optional: 'skip')"
                    )
                    return False
            if "cruise" in (leg.pickup.kind, leg.dropoff.kind):
                if not leg.cruise.port:
                    self._ask(f"detail:{index}:cruise_port", f"Cruise port for the {name}trip?")
                    return False
                if not leg.cruise.name:
                    self._ask(f"detail:{index}:cruise_name", "Cruise ship name?")
                    return False
                if f"{index}:cruise_time" not in self._detail_skips:
                    self._ask(f"detail:{index}:cruise_time", "Ship time? (e.g. 7 am; optional: 'skip')")
                    return False
            # the web form has meet_greet_choices for every pickup kind, not only airports
            if f"meet:{index}" not in self._meet_greet_done:
                return await self._ask_meet_greet(index)
        if not self.d.instructions and "0:instructions" not in self._detail_skips:
            # booking_instructions: the form pre-fills a default per pickup kind and lets the user add to it
            pickup = self.d.outbound.pickup
            default = default_instructions(pickup.kind if pickup else "city")
            self.say(f"  Driver instructions (default): {_plain(default)}")
            self._ask("detail:0:instructions", "Any special instructions to add? (optional: 'skip')")
            return False
        return True

    def _answer_detail(self, key: str, text: str) -> None:
        index_text, name = key.split(":", 1)
        leg = self.d.legs[int(index_text)]
        value = text.strip()
        skip = value.lower() in ("skip", "none", "no", "-", "n/a")
        if name == "pickup_flight":
            leg.pickup_flight.number = value
        elif name == "dropoff_flight":
            leg.dropoff_flight.number = "" if skip else value
            leg.dropoff_flight.number_skipped = skip
        elif name == "city":
            leg.pickup_flight.city = value
        elif name == "cruise_port":
            leg.cruise.port = value
        elif name == "cruise_name":
            leg.cruise.name = value
        elif name == "instructions":
            self.d.instructions = "" if skip else value
            self._detail_skips.add(f"{index_text}:instructions")
        elif name == "cruise_time":
            if not skip:
                found = parse_time(value)
                if found is None:
                    self._repeat(f"detail:{key}", "  I couldn't read that time (or 'skip').")
                    return
                hour12 = found.hour % 12 or 12
                leg.cruise.time = f"{hour12:02d}:{found.minute:02d} {'pm' if found.hour >= 12 else 'am'}"
            self._detail_skips.add(f"{index_text}:cruise_time")

    async def _ask_airline(self, index: int, end: str, question: str) -> bool:
        self.pending = f"airline_search:{index}:{end}"
        self.say(f"  {question} (name or code, e.g. AA)")
        return False

    def _search_airline(self, index: int, end: str, text: str) -> None:
        """Pick from ``BigData.airlinesData`` the way the web app's airline dropdown does."""
        airlines = list((self._common or {}).get("airlinesData") or [])
        key = f"airline_search:{index}:{end}"
        if not airlines:
            self.say("  The airline list couldn't be loaded, so airport rides can't be booked right now.")
            self._stall()
            return
        wanted = text.strip().lower()
        exact = [a for a in airlines if str(a.get("code") or "").lower() == wanted]
        matches = exact or [a for a in airlines if wanted in airline_label(a).lower()]
        leg = self.d.legs[index]
        if len(matches) == 1:
            (leg.pickup_flight if end == "pickup" else leg.dropoff_flight).airline = matches[0]
            self.say(f"  Airline: {airline_label(matches[0])}")
            return
        if not matches:
            self._repeat(key, f"  No airline matches '{text}'. Try the 2-letter code or more of the name.")
            return
        shown = matches[:_MENU_LIMIT]
        self.menu = Menu(f"airline:{index}:{end}", shown)
        self.say("  Which airline?")
        for number, airline in enumerate(shown, start=1):
            self.say(f"  {number}. {airline_label(airline)}")
        self.pending = key  # typing text searches again

    async def _ask_meet_greet(self, index: int) -> bool:
        choices = list((self._common or {}).get("meetGreets") or [])
        if not choices:
            # BigData didn't load: offer the form's two defaults rather than skipping the question
            choices = [{"id": i, "message": m} for i, m in (MEET_GREET_CITY, MEET_GREET_AIRPORT)]
        self.menu = Menu(f"meet:{index}", choices)
        leg = self.d.legs[index]
        city_pickup = leg.pickup is None or leg.pickup.kind == "city"
        default = (
            leg.meet_greet["id"]
            if leg.meet_greet
            else (MEET_GREET_CITY if city_pickup else MEET_GREET_AIRPORT)[0]
        )
        self.say("  Meet & greet (arrival instructions):")
        for number, choice in enumerate(choices, start=1):
            mark = " (default)" if choice.get("id") == default else ""
            self.say(f"  {number}. {choice.get('message')}{mark}")
        self.pending = f"meet_default:{index}"
        self.say("  Pick a number, or 'ok' for the default.")
        return False

    # --- summary and submit ---------------------------------------------------------

    def _show_summary(self, *, final: bool) -> None:
        d = self.d
        if d.outbound.pickup is None:
            self.say("  Nothing collected yet.")
            return
        sym = d.currency_symbol
        self.say("  ---- Booking summary ----")
        service = _SERVICE_NAMES.get(d.service_type or "", "-")
        if d.is_charter:
            service += f", {d.hours} hours"
        self.say(f"  Service:    {service}")
        for index, leg in enumerate(d.legs):
            title = "Return" if index else "Pickup"
            when = f"{_day(leg.date)} {_clock(leg.time)}" if leg.date and leg.time else "-"
            self.say(f"  {title}:     {when}")
            self.say(f"    from      {leg.pickup.short if leg.pickup else '-'}")
            for stop in leg.stops:
                self.say(f"    stop      {stop.short}")
            self.say(f"    to        {leg.dropoff.short if leg.dropoff else '-'}")
            if leg.route:
                miles, minutes = leg.route.meters / 1609.344, round(leg.route.seconds / 60)
                self.say(f"    distance  {miles:.1f} mi, about {minutes} min")
            if leg.pickup and leg.pickup.kind == "airport" and leg.pickup_flight.airline:
                flight = leg.pickup_flight
                self.say(f"    arriving  {airline_label(flight.airline)} {flight.number} from {flight.city}")
            if leg.dropoff and leg.dropoff.kind == "airport" and leg.dropoff_flight.airline:
                flight = leg.dropoff_flight
                self.say(
                    f"    departing {airline_label(flight.airline)} {flight.number or '(no flight number)'}"
                )
            if leg.cruise.port:
                self.say(f"    cruise    {leg.cruise.name} at {leg.cruise.port} ({leg.cruise.time})")
            if leg.meet_greet:
                self.say(f"    meet      {leg.meet_greet.get('message')}")
        pax = d.pax if d.pax is not None else "-"
        bags = d.luggage if d.luggage is not None else "-"
        self.say(f"  Passengers: {pax}   Bags: {bags}")
        if d.vehicle:
            v = d.vehicle
            details = v.get("vehicle_details") or {}
            title = " ".join(
                str(x) for x in (details.get("year"), details.get("make"), details.get("model")) if x
            )
            category = d.category.get("name") if d.category else v.get("name")
            self.say(f"  Vehicle:    {title or v.get('name')} ({category})")
            if self.is_admin and not d.is_master_vehicle:
                operator = v.get("affiliate_company") or v.get("affiliate_name")
                if operator:
                    self.say(
                        f"  Operator:   {operator}"
                        + (f" ({v.get('affiliate_phone')})" if v.get("affiliate_phone") else "")
                    )
            driver = v.get("driverInformation") or {}
            if driver.get("name"):
                extra = f", {driver.get('gender')}" if driver.get("gender") else ""
                if self.is_admin:
                    contact = " ".join(
                        str(x)
                        for x in (driver.get("cell_isd"), driver.get("cell_number"), driver.get("email"))
                        if x
                    )
                    extra += f" — {contact}" if contact else ""
                self.say(f"  Driver:     {driver.get('name')}{extra}")
            if v.get("cancellation_policy") not in (None, ""):
                self.say(f"  Cancel:     free up to {v.get('cancellation_policy')} hours before pickup")
        if d.customer:
            self.say(f"  Customer:   {d.customer.label}")
        if d.passenger and d.passenger.name:
            p = d.passenger
            contact = " ".join(x for x in (p.cell_isd + " " + p.cell if p.cell else "", p.email) if x)
            self.say(f"  Passenger:  {p.name}" + (f" ({contact})" if contact else ""))
        if d.instructions:
            self.say(f"  Notes:      {d.instructions}")
        if d.rates:
            self._print_rates("Price", d.rates, sym)
            if d.return_rates:
                self._print_rates("Return price", d.return_rates, sym)
                total = d.rates.grand_total + d.return_rates.grand_total
                self.say(f"  Trip total: {_money(sym, total)}")
            listed = (d.vehicle or {}).get(f"rate_breakdown_{d.service_type}", {}).get("grand_total")
            if listed is not None and _float(listed) is not None:
                saved = d.rates.grand_total + (d.return_rates.grand_total if d.return_rates else 0)
                if abs(_float(listed) - saved) >= 0.01:
                    self.say(
                        f"  (The vehicle list showed {_money(sym, listed)}; the booking saves the price "
                        "above, calculated the way the web app does.)"
                    )
        self.say("  Payment:    none now; the booking is saved unpaid and paid later, as in the web app.")
        if final and not self.is_admin and d.outbound.date and d.outbound.time:
            at = datetime.combine(
                d.outbound.date, datetime.strptime(d.outbound.time, "%H:%M:%S").time(), self.now().tzinfo
            )
            if at - self.now() <= timedelta(hours=24):
                self.say("  Note:       this pickup is within 24 hours.")

    def _print_rates(self, title: str, rates: Any, sym: str) -> None:
        labels = []
        for group in rates.lines.values():
            for name, line in group.items():
                amount = rates.totals.get(name, 0)
                if amount:
                    labels.append(f"{line.get('rate_label') or name.replace('_', ' ')} {_money(sym, amount)}")
        self.say(
            f"  {title}:"
            + (" " * max(1, 11 - len(title)))
            + f"{_money(sym, rates.grand_total)}"
            + (f"  ({', '.join(labels)})" if labels else "")
        )

    async def _answer_confirm(self, text: str) -> None:
        answer = parse_yes_no(text)
        if answer is None:
            self._repeat("confirm", "  Please answer yes to book, or no.")
            return
        self._awaiting_confirm = False
        if not answer:
            self.say("  Not booked. Change something (e.g. 'change passengers to 3'), 'back', or 'cancel'.")
            self._stall()
            return
        await self._submit()
        self._stall()  # after a failure, wait for a fix; the next confirm needs a new explicit yes

    async def _submit(self) -> None:
        d = self.d
        await self._common_data()
        try:
            payload = submit_payload(
                d,
                mode=self.mode,
                flags=self._flags(),
                vehicle_categories=(self._common or {}).get("vehicleCategories"),
            )
        except (RatesError, ValueError) as exc:
            self.say(f"  Can't build the booking: {exc}. Nothing was sent.")
            return
        if not payload.get("vehicle_type"):
            self.say(
                "  This vehicle has no vehicle type, so the web form would refuse it. Pick another ('back')."
            )
            return
        self.say("  Booking...")
        try:
            answer = await self.api.post(self.paths.create_reservation, payload)
        except ApiUnreachableError as exc:
            self.say(f"  {exc}. I can't tell whether the booking was created, so I won't resend it.")
            self.say("  Check the bookings list first. To try again anyway, say 'summary' and confirm.")
            return
        except ApiError as exc:
            self.say(f"  The booking was not created: {exc.plain()}")
            self.say(
                "  Fix it (e.g. 'change passenger phone to ...') and confirm again; "
                "nothing is resent on its own."
            )
            return
        data = answer.get("data") or {}
        self._booked = data
        line = f"  Booked. Reservation #{data.get('reservation_id', '?')}"
        if data.get("order_id"):
            line += f" (order {data['order_id']})"
        if data.get("return_reservation_id"):
            line += f"; return #{data['return_reservation_id']}"
            if data.get("return_order_id"):
                line += f" (order {data['return_order_id']})"
        self.say(line + ".")
        self.say("  Confirmation emails and texts go out from the server as usual.")
        customer = d.customer
        if self.is_admin and customer is not None and customer.account_type == "loose_customer":
            self._ask(
                "card",
                f"Add a payment card for {customer.label.removesuffix(' (new customer)')} now? "
                "A secure form opens in your browser. (yes/no)",
            )
            return
        if customer is not None and customer.travel_client_acc == "travel_loose_customer":
            self.say(
                "  There's no API to add a card to a travel agent's client, so the client has no card yet."
            )
        self.finished = True

    async def _add_card_for_new_customer(self) -> None:
        reservation_id = (self._booked or {}).get("reservation_id")
        acc_id = None
        if self.account_for_reservation is not None and reservation_id:
            acc_id = await self.account_for_reservation(int(reservation_id))
        if not acc_id:
            self.say(
                "  Couldn't find the new customer's account, so no card was added. Use ':card ACC_ID' later."
            )
            return
        result = await self.collect_card(
            lambda fields: add_card(self.api, self.paths.add_credit_card, fields, acc_id=acc_id),
            title="Add a card for the new customer",
            default_name=self.d.passenger.name if self.d.passenger else "",
            say=self.say,
        )
        self.say(f"  {result.message}")

    def _today(self) -> date:
        return self.now().date()


async def add_card(api: LimoApi, path: str, fields: dict[str, Any], *, acc_id: Any = None) -> str:
    """POST the role's add-credit-card, shaped like new-card-form.component.ts:79."""
    body: dict[str, Any] = {"id": ""}
    if acc_id is not None:
        body["acc_id"] = acc_id
    body.update(fields)
    answer = await api.post(path, body)
    return str(answer.get("message") or "Credit card has been added successfully.")


def _passenger_from(data: dict[str, Any]) -> Passenger:
    """``autofillData('passenger', ...)`` (booking.ts:6852)."""
    names = [data.get("first_name"), data.get("middle_name"), data.get("last_name")]
    isd = str(data.get("mobileIsd") or "+1")
    return Passenger(
        name=" ".join(str(n) for n in names if n),
        email=str(data.get("email") or ""),
        cell=str(data.get("mobile") or ""),
        cell_isd=isd if isd.startswith("+") else f"+{isd}",
        cell_country=str(data.get("mobileCountry") or "us"),
    )


def _account_label(row: dict[str, Any]) -> str:
    if row.get("name"):
        return str(row["name"])
    return (
        " ".join(str(row.get(k) or "") for k in ("first_name", "last_name")).strip()
        or f"account {row.get('id')}"
    )


def _phone(text: str, default_isd: str = "+1") -> tuple[str, str] | None:
    """'+91 98765 43210' -> ('+91', '9876543210'); without a + the default ISD is used."""
    cleaned = re.sub(r"[\s().-]", "", text)
    if cleaned.startswith("+"):
        for length in (4, 3, 2):
            isd = cleaned[:length]
            if isd in _ISD_COUNTRIES and cleaned[length:].isdigit():
                return isd, cleaned[length:] if 4 <= len(cleaned[length:]) <= 15 else None  # type: ignore[return-value]
        match = re.fullmatch(r"(\+\d{1,3})(\d{4,15})", cleaned)
        return (match.group(1), match.group(2)) if match else None
    if cleaned.isdigit() and 4 <= len(cleaned) <= 15:
        return default_isd, cleaned
    return None


def _slot_name(slot: str) -> str:
    return {
        "pickup": "pickup", "dropoff": "drop-off", "return_pickup": "return pickup",
        "return_dropoff": "return drop-off", "stop": "stop", "return_stop": "return stop",
    }.get(slot, slot)  # fmt: skip


def _day(day: date | None) -> str:
    return day.strftime("%a %d %b %Y") if day else "-"


def _clock(value: str | None) -> str:
    if not value:
        return "-"
    return datetime.strptime(value, "%H:%M:%S").strftime("%I:%M %p").lstrip("0")


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _plain(html_text: str) -> str:
    """'<ol><li>A</li><li>B</li></ol>' -> 'A / B'."""
    items = [re.sub(r"<[^>]+>", "", part).strip() for part in re.split(r"</li>", html_text)]
    return " / ".join(item for item in items if item)


def _money(symbol: str, value: Any) -> str:
    number = _float(value)
    return f"{symbol}{number:,.2f}" if number is not None else "-"
