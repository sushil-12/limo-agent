"""What the SQL-writing model knows about the database.

Tables, columns, types and enum values come from the live database (cached). The
curated notes below add what names alone don't say: what a table means, which
columns matter, and how tables join. Only the tables picked for a question go into
the prompt, which keeps it small enough for a local model to answer quickly.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

_MAX_ENUM_VALUES = 20
_RELATIVE_CUTOFF = 0.4  # drop tables scoring under 40% of the best match
# Bookings that need no more work: excluded from "active", "upcoming" and "needs work".
CLOSED_STATUSES = "'cancelled', 'rejected', 'refund', 'finalized', 'comped', 'non_payable'"
_COLUMNS_SQL = text(
    """
    SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
    ORDER BY TABLE_NAME, ORDINAL_POSITION
    """
)
_WORD = re.compile(r"[a-z0-9]+")
_ENUM = re.compile(r"^enum\((.*)\)$", re.IGNORECASE | re.DOTALL)
_ENUM_VALUE = re.compile(r"'((?:[^']|'')*)'")


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class TableNotes:
    """Hand-written guidance for one table."""

    description: str
    keywords: tuple[str, ...] = ()
    key_columns: tuple[str, ...] = ()  # shown with types and notes; the rest by name only
    column_notes: Mapping[str, str] = field(default_factory=dict)
    joins: tuple[str, ...] = ()
    list_columns: tuple[str, ...] = ()  # text lists of numbers; the guard rejects = / LIKE on them
    companions: tuple[str, ...] = ()  # picked along with this table: answers usually need them


NOTES: dict[str, TableNotes] = {
    "tbl_reservations": TableNotes(
        "THE bookings table (reservations, rides, trips). One row per booking (one leg of a trip). "
        f"Closed bookings: booking_status IN ({CLOSED_STATUSES}).",
        keywords=(
            "booking",
            "reservation",
            "ride",
            "trip",
            "pending",
            "cancelled",
            "upcoming",
            "pickup",
            "dropoff",
            "airport",
            "revenue",
            "fare",
            "quote",
            "status",
            "assigned",
            "unassigned",
            "completed",
            "work",
        ),
        key_columns=(
            "id",
            "acc_id",
            "account_type",
            "travel_client_id",
            "affiliate_id",
            "lose_affiliate",
            "driver_id",
            "is_pending_affiliate",
            "vehicle_type",
            "booking_status",
            "payment_status",
            "service_type",
            "transfer_type",
            "pickup_date",
            "pickup_time",
            "pickup",
            "pickup_address",
            "dropoff",
            "dropoff_address",
            "total_passengers",
            "charged_amount",
            "share_array",
            "status",
            "created_by_role",
            "created_at",
        ),
        column_notes={
            "id": "THE booking number shown to people (#2096). conf_id is always empty: never use it",
            "acc_id": "the client who booked, = tbl_account.id",
            "account_type": "'individual' or 'travel_planner' (booked by a travel agent)",
            "travel_client_id": (
                "the passenger a travel agent booked for, = tbl_account.id. Not the user: never filter on it "
                "for 'my' bookings"
            ),
            # Riders don't see these columns, so the assignment rule lives in their notes.
            "affiliate_id": (
                "affiliate serving it, = tbl_account.id = tbl_affiliate_company.acc_id. A booking is "
                "served by EITHER affiliate_id OR lose_affiliate; empty = NULL or 0. "
                "Needs work / unassigned = COALESCE(affiliate_id, 0) = 0 AND "
                "COALESCE(lose_affiliate, 0) = 0 (AND, never OR) AND not closed AND "
                "pickup_date >= CURDATE() (drop only the date filter when past ones are asked for)"
            ),
            "lose_affiliate": "external operator entered by admin, = tbl_lose_affiliates.id",
            "driver_id": (
                "affiliate's driver, = tbl_driver_setting.id. Always NULL for lose-affiliate bookings, so an "
                "empty driver_id does NOT mean unassigned"
            ),
            "is_pending_affiliate": "1 = the affiliate has not finished onboarding",
            "vehicle_type": "= tbl_vehicle_categories.id",
            "booking_status": (
                "trip progress and payment state mixed. Completed rides: 'passenger_drop', 'ended', "
                "'finalized', 'paid', 'paid_cash', 'partial_paid'"
            ),
            "payment_status": "'paid', 'unpaid', 'partial_paid', 'refund', 'partial_refund', 'paid_cash'",
            "pickup_date": "scheduled pickup day; upcoming = pickup_date >= CURDATE()",
            "pickup": (
                "blank for airport pickups: always select COALESCE(NULLIF(pickup, ''), pickup_address). "
                "Same for dropoff. Pickups in a place (city, airport, street): "
                "CONCAT_WS(' ', pickup, pickup_address) LIKE '%chicago%'. Airport codes start the text "
                "('ORD - Chicago Ohare Intl'): match them as COALESCE(NULLIF(pickup, ''), pickup_address) "
                "LIKE 'ORD %', never '%ORD%' (matches Oxford)"
            ),
            "transfer_type": (
                "'airport_to_city', 'city_to_airport', 'city_to_city', 'airport_to_airport', "
                "'city_to_cruise', 'cruise_to_city', 'cruise_to_airport'"
            ),
            "charged_amount": "amount PRE-charged to the customer, not the booking total",
            "share_array": (
                "JSON split of the money (MariaDB: no -> / ->> operator): "
                "JSON_UNQUOTE(JSON_EXTRACT(share_array, '$.affiliateShare')) = what the affiliate gets; also "
                "'$.adminShare' (admin profit), '$.travelAgentShare', '$.grandTotal', '$.baseRate', "
                "'$.stripeFee', '$.deducted_admin_share'. A missing key = 0 (COALESCE). Read and SUM the "
                "stored values, never compute one share from others. Can be empty"
            ),
            "status": "'enable' = active row, 'disable' = deleted; filter status = 'enable'",
            "created_at": "when the booking was made",
        },
        joins=(
            "tbl_reservations.acc_id = tbl_account.id",
            "tbl_reservations.travel_client_id = tbl_account.id",
            "tbl_reservations.driver_id = tbl_driver_setting.id",
            "tbl_reservations.vehicle_type = tbl_vehicle_categories.id",
            "tbl_reservations.affiliate_id = tbl_affiliate_company.acc_id",
            "tbl_reservations.lose_affiliate = tbl_lose_affiliates.id",
            "tbl_order.reservation_id = tbl_reservations.id",
            "tbl_travel_planner.acc_id = tbl_reservations.acc_id",
        ),
    ),
    "tbl_account": TableNotes(
        "Profiles of every person (customers, drivers, affiliates, agents). user_id = users.id.",
        keywords=(
            "customer",
            "client",
            "account",
            "profile",
            "rider",
            "passenger",
            "name",
            "city",
            "signup",
            "address",
            "located",
            "location",
        ),
        key_columns=(
            "id",
            "user_id",
            "first_name",
            "last_name",
            "email",
            "mobile",
            "address",
            "city",
            "state",
            "zip",
            "country",
            "status",
            "created_at",
        ),
        column_notes={
            "address": (
                "full street address with city, e.g. '2411 W Fargo Ave, Chicago, IL 60645, USA'. City/area "
                "searches: CONCAT_WS(' ', address, street, city, state, zip) LIKE '%chicago%'"
            ),
            "city": "often holds the county ('Cook County'), never search it alone; search address too",
        },
        joins=("tbl_account.user_id = users.id",),
    ),
    "users": TableNotes(
        "Login accounts. role = tbl_user_roles.id (1 admin, 2 driver, 3 travel agent, 4 corporate, "
        "5 rider, 6 sub admin, 7 sub travel agent, 8 sub affiliate, 9 subscriber, 10 affiliate driver).",
        keywords=("user", "login", "role", "registered", "signup", "admin", "subscriber"),
        key_columns=(
            "id",
            "name",
            "email",
            "phone",
            "role",
            "created_from",
            "last_login_at",
            "soft_delete",
            "created_at",
        ),
        column_notes={"soft_delete": "1 = deleted"},
        joins=("users.id = tbl_account.user_id", "users.role = tbl_user_roles.id"),
    ),
    "tbl_user_roles": TableNotes("Role names for users.role.", keywords=("role",)),
    "tbl_driver_setting": TableNotes(
        "Drivers (chauffeurs) of affiliates. acc_id = their AFFILIATE's tbl_account.id. Phone = cell_isd + "
        "cell_number. Who drives a booking: driver_id set -> this table; else is_pending_affiliate = 1 -> "
        "tbl_affiliate_company; else lose_affiliate set -> tbl_lose_affiliates; else unassigned.",
        keywords=("driver", "chauffeur", "rating", "license", "suspended"),
        key_columns=(
            "id",
            "acc_id",
            "first_name",
            "last_name",
            "email",
            "cell_isd",
            "cell_number",
            "status",
            "starRatingValue",
            "created_at",
        ),
        column_notes={"status": "'enable', 'suspend' or 'disable'", "starRatingValue": "average rating"},
        joins=(
            "tbl_driver_setting.acc_id = tbl_account.id",
            "tbl_reservations.driver_id = tbl_driver_setting.id",
        ),
    ),
    "tbl_affiliate_company": TableNotes(
        "Affiliates: registered limo companies that serve rides; they log in with their tbl_account. "
        "acc_id = tbl_reservations.affiliate_id. Phone = cell_isd + cell_number "
        "(else dispatchisd + dispatch). Affiliates in a place = badge city OR their account address: "
        "(bc.name LIKE '%chicago%' OR CONCAT_WS(' ', a.address, a.street, a.city, a.state, a.zip) "
        "LIKE '%chicago%'), joining tbl_account a ON a.id = acc_id and LEFT JOIN tbl_badge_cities bc "
        "ON bc.id = badge_city. Label each match as area: 'X badge, based in X' (both), 'X badge, based "
        "outside X' (badge only, e.g. a suburb), 'Based in X, no X badge' (address only); town = "
        "TRIM(SUBSTRING_INDEX(SUBSTRING_INDEX(a.address, ',', -3), ',', 1)). Break counts down by area "
        "and account_approval.",
        keywords=(
            "affiliate",
            "company",
            "operator",
            "fleet",
            "partner",
            "approval",
            "approved",
            "step",
            "onboarding",
            "registration",
        ),
        key_columns=(
            "id",
            "acc_id",
            "company_name",
            "affiliate_type",
            "badge_city",
            "cell_isd",
            "cell_number",
            "dispatchisd",
            "dispatch",
            "step_completed",
            "account_approval",
            "approved",
            "created_at",
        ),
        column_notes={
            # Web saves ["0","1"], mobile saves [0,1,3,4] (and can skip steps); spacing varies
            # and some rows are NULL or not valid JSON. A digit-boundary REGEXP matches every
            # form and never errors, unlike = / LIKE / JSON_CONTAINS.
            "step_completed": (
                "onboarding steps 0-6 already done, stored as text like '[\"0\",\"1\"]' or '[0,1,3]'. "
                "NEVER compare with = or LIKE. Step 3 done: "
                "step_completed REGEXP '(^|[^0-9])3([^0-9]|$)' (put the step's digit where 3 is). "
                "'on step 3' = step 2 done AND step 3 not done (NOT REGEXP). "
                "All 7 steps done = account_approval 'accepted'. Onboarding affiliates = "
                "account_approval = 'in-progress' (no REGEXP needed)"
            ),
            "account_approval": (
                "THE approval status: 'in-progress' (onboarding), 'completed', 'accepted' (approved), "
                "'rejected'"
            ),
            "approved": "misleading, NEVER use it; use account_approval",
            "badge_city": (
                "= tbl_badge_cities.id stored as text (a number, NEVER a city name): the city where they "
                "hold a chauffeur badge"
            ),
        },
        list_columns=("step_completed",),
        joins=(
            "tbl_affiliate_company.acc_id = tbl_account.id",
            "tbl_reservations.affiliate_id = tbl_affiliate_company.acc_id",
            "tbl_affiliate_company.badge_city = tbl_badge_cities.id",
        ),
        companions=("tbl_account", "tbl_badge_cities"),
    ),
    "tbl_badge_cities": TableNotes(
        "City names for tbl_affiliate_company.badge_city (id -> name). Match cities by name, never by id.",
        keywords=("badge", "city"),
        key_columns=("id", "name", "status"),
    ),
    "tbl_vehicle_categories": TableNotes(
        "Vehicle types customers book (sedan, SUV, stretch limo, ...).",
        keywords=(
            "vehicle",
            "car",
            "category",
            "type",
            "sedan",
            "suv",
            "limo",
            "limousine",
            "van",
            "bus",
            "seat",
        ),
        key_columns=("id", "vehicle_cat_name", "seats", "luggage", "status"),
        joins=("tbl_reservations.vehicle_type = tbl_vehicle_categories.id",),
    ),
    "tbl_vehicles": TableNotes(
        "Vehicles owned by affiliates. affiliate_id = owner's tbl_account.id.",
        keywords=("vehicle", "car", "fleet", "plate", "license_plate", "seat"),
        key_columns=(
            "id",
            "affiliate_id",
            "vehicle_type",
            "year",
            "color",
            "license_plate",
            "seats",
            "luggage",
            "status",
            "created_at",
        ),
        joins=("tbl_vehicles.affiliate_id = tbl_account.id",),
    ),
    "tbl_invoices": TableNotes(
        "Invoices, one per booking.",
        keywords=("invoice", "billing", "bill", "paid", "unpaid", "open"),
        joins=("tbl_invoices.reservation_id = tbl_reservations.id",),
    ),
    "tbl_order": TableNotes(
        "One order per booking: grand_total = the booking's total price. Currency: UPPER(currency) "
        "(USD, EUR, GBP, ...). Never add up different currencies: GROUP BY UPPER(currency). "
        "id is the ORDER id, NOT the booking number: find a booking's order by reservation_id.",
        keywords=(
            "order",
            "total",
            "subtotal",
            "revenue",
            "amount",
            "price",
            "sales",
            "earning",
            "currency",
            "rate",
            "tax",
            "breakdown",
            "charges",
        ),
        key_columns=("id", "reservation_id", "grand_total", "currency", "created_at"),
        joins=(
            "tbl_order.reservation_id = tbl_reservations.id",
            "tbl_order_details.order_id = tbl_order.id",
        ),
    ),
    "tbl_order_details": TableNotes(
        "Price breakdown lines of an order. rate_label = line name (Base Rate, Tolls, ...). Only show rows "
        "with amount <> 0. Lines do not add up to grand_total (pre-markup): never explain the gap. "
        "A booking's lines: JOIN tbl_order o ON o.id = order_id WHERE o.reservation_id = <booking>.",
        keywords=("breakdown", "base", "rate", "tax", "toll", "gratuity", "parking", "stop", "amenities"),
        key_columns=("id", "order_id", "rate_category", "rate_label", "amount"),
        column_notes={
            "order_id": "= tbl_order.id, NEVER a booking number",
            "rate_category": (
                "'all_inclusive_rates', 'amenities', 'taxes', 'direct_taxes', 'misc'. Taxes = "
                "rate_category IN ('taxes', 'direct_taxes')"
            ),
        },
        joins=("tbl_order_details.order_id = tbl_order.id",),
    ),
    "tbl_lose_affiliates": TableNotes(
        "Lose affiliates: external one-off operators an admin enters for a booking (no app login). "
        "id = tbl_reservations.lose_affiliate. Phone = phone_isd + phone.",
        keywords=("lose", "loose", "external", "operator", "outside", "farm", "farmout"),
        key_columns=("id", "reservation_id", "name", "operator_name", "phone_isd", "phone", "email", "city"),
        joins=("tbl_reservations.lose_affiliate = tbl_lose_affiliates.id",),
    ),
    "tbl_payment_details": TableNotes(
        "Payments and refunds recorded against bookings.",
        keywords=("payment", "paid", "refund", "charge", "revenue", "amount", "card"),
        key_columns=(
            "id",
            "reservation_id",
            "amount",
            "paid_amount",
            "refund_amount",
            "payment_method",
            "gateway_name",
            "created_at",
        ),
        joins=("tbl_payment_details.reservation_id = tbl_reservations.id",),
    ),
    "tbl_passengers": TableNotes(
        "Passengers named on a booking (may differ from the customer who booked).",
        keywords=("passenger", "traveler", "guest"),
        joins=("tbl_passengers.reservation_id = tbl_reservations.id",),
    ),
    "tbl_driver_feedback": TableNotes(
        "Customer ratings (1-5) and comments about the driver after a trip.",
        keywords=("feedback", "rating", "review", "star", "comment"),
        joins=(
            "tbl_driver_feedback.booking_id = tbl_reservations.id",
            "tbl_driver_feedback.driver_id = tbl_driver_setting.id",
        ),
    ),
    "tbl_booking_status_history": TableNotes(
        "Every booking status change, oldest first.",
        keywords=("history", "status", "changed", "timeline"),
    ),
    "tbl_travel_planner": TableNotes(
        "Travel agents (agencies). Join on acc_id (= agent's tbl_account.id), NEVER on id. Onboarding has "
        "steps 1-2; account_approval e.g. 'accepted', 'pending'.",
        keywords=("travel", "agent", "agency", "planner"),
        joins=(
            "tbl_travel_planner.acc_id = tbl_account.id",
            "tbl_travel_planner.acc_id = tbl_reservations.acc_id",
        ),
    ),
    "tbl_airports": TableNotes("Airport reference list.", keywords=("airport", "iata", "code")),
    "tbl_airlines": TableNotes("Airline reference list.", keywords=("airline", "flight", "carrier")),
    "mobile_bookings": TableNotes(
        "Legacy mobile-app booking records. NOT the bookings source of truth: use tbl_reservations.",
    ),
}


Schema = dict[str, tuple[Column, ...]]


class SchemaCatalog:
    """Loads the live schema once and refreshes it after ``ttl_seconds``."""

    def __init__(self, engine: AsyncEngine, *, ttl_seconds: float) -> None:
        self._engine = engine
        self._ttl = ttl_seconds
        self._schema: Schema | None = None
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    async def schema(self) -> Schema:
        """Table name -> columns for every table the DB user can see."""
        if self._schema is not None and time.monotonic() - self._loaded_at < self._ttl:
            return self._schema
        async with self._lock:
            if self._schema is None or time.monotonic() - self._loaded_at >= self._ttl:
                async with self._engine.connect() as conn:
                    self._schema = await conn.run_sync(_reflect)
                self._loaded_at = time.monotonic()
        return self._schema


def _reflect(conn: Connection) -> Schema:
    """On MySQL, information_schema lists only columns this DB user was granted, so column
    grants hide columns from the model too. Other dialects (tests) use the inspector."""
    if conn.dialect.name == "mysql":
        schema: dict[str, list[Column]] = {}
        for table, column, column_type in conn.execute(_COLUMNS_SQL):
            schema.setdefault(table, []).append(Column(column, column_type))
        return {table: tuple(columns) for table, columns in schema.items()}
    inspector = inspect(conn)
    return {
        table: tuple(Column(c["name"], _type_text(c["type"])) for c in inspector.get_columns(table))
        for table in inspector.get_table_names()
    }


def _type_text(sql_type: Any) -> str:
    enums = getattr(sql_type, "enums", None)
    if enums:
        values = ",".join(f"'{v}'" for v in enums[:_MAX_ENUM_VALUES])
        return f"enum({values}{',...' if len(enums) > _MAX_ENUM_VALUES else ''})"
    try:
        return str(sql_type).lower()
    except Exception:  # some dialect types cannot render without a dialect
        return type(sql_type).__name__.lower()


def enum_values(type_text: str) -> frozenset[str] | None:
    """Values of an ``enum('a','b')`` column type; None for other or truncated types."""
    match = _ENUM.match(type_text)
    if match is None or match.group(1).endswith("..."):
        return None
    return frozenset(v.replace("''", "'") for v in _ENUM_VALUE.findall(match.group(1)))


def words(text: str) -> set[str]:
    """Lower-case words plus naive singulars, so "bookings" matches "booking"."""
    found = set(_WORD.findall(text.lower()))
    return found | {w[:-1] for w in found if len(w) > 3 and w.endswith("s")}


def select_tables(question: str, tables: Mapping[str, Iterable[str]], *, limit: int) -> list[str]:
    """The tables most relevant to ``question``, given each table's visible column names.

    Scoring is keyword overlap: curated keywords count most, then words in the table
    name, then column names; undocumented tables count half. Tables far below the best
    match are dropped. With no match, bookings are the usual subject.
    """
    if len(tables) <= limit:
        return sorted(tables)
    asked = words(question)
    scores: dict[str, float] = {}
    for name, columns in tables.items():
        notes = NOTES.get(name)
        score = 3.0 * len(asked & set(notes.keywords)) if notes else 0.0
        score += 2.0 * len(asked & words(name.removeprefix("tbl_").replace("_", " ")))
        column_words: set[str] = set()
        for column in columns:
            column_words |= words(column.replace("_", " "))
        score += min(2.0, 0.5 * len(asked & column_words))
        score = score + 0.1 if notes is not None and notes.keywords else score * 0.5
        if score >= 1.0:
            scores[name] = score
    if not scores:
        return ["tbl_reservations"] if "tbl_reservations" in tables else []
    cutoff = _RELATIVE_CUTOFF * max(scores.values())
    ranked = sorted((n for n in scores if scores[n] >= cutoff), key=lambda n: (-scores[n], n))
    picked: list[str] = []
    for name in ranked:
        notes = NOTES.get(name)
        for table in (name, *(notes.companions if notes else ())):
            if table in tables and table not in picked:
                picked.append(table)
    return picked[:limit]


def render_tables(tables: Collection[str], columns: Mapping[str, Collection[Column]]) -> str:
    """Compact schema text for the prompt."""
    blocks: list[str] = []
    for name in tables:
        notes = NOTES.get(name)
        cols = {c.name: c for c in columns[name]}
        key = [c for c in (notes.key_columns if notes else ()) if c in cols] or list(cols)[:25]
        lines = [f"TABLE {name}" + (f" -- {notes.description}" if notes else "")]
        for col in key:
            note = notes.column_notes.get(col) if notes else None
            lines.append(f"  {col} {cols[col].type}" + (f" -- {note}" if note else ""))
        rest = [c for c in cols if c not in key]
        if rest:
            lines.append("  other columns: " + ", ".join(rest))
        for join in notes.joins if notes else ():
            sides = [side.strip().split(".") for side in join.split("=")]
            if all(table in tables and col in {c.name for c in columns[table]} for table, col in sides):
                lines.append(f"  join: {join}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
