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


NOTES: dict[str, TableNotes] = {
    "tbl_reservations": TableNotes(
        "THE bookings table (reservations, rides, trips). One row per booking.",
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
            "completed",
        ),
        key_columns=(
            "id",
            "conf_id",
            "acc_id",
            "driver_id",
            "affiliate_id",
            "vehicle_type",
            "booking_status",
            "payment_status",
            "service_type",
            "transfer_type",
            "pickup_date",
            "pickup_time",
            "pickup",
            "dropoff",
            "total_passengers",
            "quote_amount",
            "charged_amount",
            "affiliate_charged_amount",
            "status",
            "created_at",
        ),
        column_notes={
            "conf_id": "confirmation number shown to customers",
            "acc_id": "customer, = tbl_account.id",
            "driver_id": "= tbl_driver_setting.id",
            "affiliate_id": "affiliate/fleet operator, = tbl_account.id (tbl_affiliate_company.acc_id)",
            "vehicle_type": "= tbl_vehicle_categories.id",
            "booking_status": "trip progress; 'ended'/'finalized' mean completed",
            "pickup_date": "scheduled pickup day; upcoming = pickup_date >= CURDATE()",
            "pickup": "pickup place name; pickup_address is the full address",
            "quote_amount": "price quoted to the customer",
            "charged_amount": "amount charged to the customer",
            "affiliate_charged_amount": "amount paid to the affiliate/driver",
            "status": "'enable' = active row, 'disable' = deleted; filter status = 'enable'",
            "created_at": "when the booking was made",
        },
        joins=(
            "tbl_reservations.acc_id = tbl_account.id",
            "tbl_reservations.driver_id = tbl_driver_setting.id",
            "tbl_reservations.vehicle_type = tbl_vehicle_categories.id",
            "tbl_reservations.affiliate_id = tbl_affiliate_company.acc_id",
        ),
    ),
    "tbl_account": TableNotes(
        "Profiles of every person (customers, drivers, affiliates, agents). user_id = users.id.",
        keywords=("customer", "client", "account", "profile", "rider", "passenger", "name", "city", "signup"),
        key_columns=(
            "id",
            "user_id",
            "first_name",
            "last_name",
            "email",
            "mobile",
            "city",
            "state",
            "country",
            "status",
            "created_at",
        ),
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
        "Drivers (chauffeurs). acc_id = the driver's tbl_account.id.",
        keywords=("driver", "chauffeur", "rating", "license", "suspended"),
        key_columns=(
            "id",
            "acc_id",
            "first_name",
            "last_name",
            "email",
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
        "Affiliates: limo companies/operators that fulfil rides. acc_id = owner's tbl_account.id.",
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
            "approved",
            "step_completed",
            "account_approval",
            "created_at",
        ),
        column_notes={
            # Web saves ["0","1"], mobile saves [0,1,3,4] (and can skip steps); spacing varies
            # and some rows are NULL or not valid JSON. A digit-boundary REGEXP matches every
            # form and never errors, unlike = / LIKE / JSON_CONTAINS.
            "step_completed": (
                "onboarding steps 0-6 already done, stored as text like '[\"0\",\"1\"]' or '[0,1,3]'. "
                "NEVER compare with = or LIKE. Step N done: "
                "step_completed REGEXP '(^|[^0-9])N([^0-9]|$)'. "
                "'on step N' = step N-1 done AND step N not done (NOT REGEXP). "
                "All 7 steps done = account_approval 'accepted'"
            ),
            "account_approval": "e.g. 'in-progress' while onboarding, 'accepted' once approved",
        },
        list_columns=("step_completed",),
        joins=(
            "tbl_affiliate_company.acc_id = tbl_account.id",
            "tbl_reservations.affiliate_id = tbl_affiliate_company.acc_id",
        ),
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
        "Price breakdown totals per booking.",
        keywords=("order", "total", "subtotal", "revenue", "amount", "price"),
        joins=("tbl_order.reservation_id = tbl_reservations.id",),
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
        "Travel agents (agencies). acc_id = agent's tbl_account.id.",
        keywords=("travel", "agent", "agency", "planner"),
        joins=("tbl_travel_planner.acc_id = tbl_account.id",),
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
    return ranked[:limit]


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
            left, right = (side.strip().split(".")[0] for side in join.split("="))
            if left in tables and right in tables:
                lines.append(f"  join: {join}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
