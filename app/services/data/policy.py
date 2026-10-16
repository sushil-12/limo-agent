"""Which tables, columns and rows a requester may query.

- admin / sub_admin: every table except ``DENIED_TABLES``, all non-sensitive columns.
- rider: their own bookings (``tbl_reservations.acc_id``) and what hangs off them.
- driver / affiliate_driver: bookings assigned to them (``driver_id``) or to their
  company (``affiliate_id``), their vehicles and their ratings.
- every other role and guests: no data access (their ownership rules aren't modelled yet).

Scopes are SQL conditions built here from ids looked up in the database, never from
model output. ``sql_guard`` applies them to every table reference.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlglot import exp

from app.services.requester import Requester

FULL_ACCESS_ROLES = frozenset({"admin", "sub_admin"})
RIDER_ROLES = frozenset({"rider"})
DRIVER_ROLES = frozenset({"driver", "affiliate_driver"})
DATA_ROLES = FULL_ACCESS_ROLES | RIDER_ROLES | DRIVER_ROLES

# Credentials, sessions, raw logs and payment instruments: never queryable, even by admins.
DENIED_TABLES = frozenset(
    {
        "oauth_access_tokens", "oauth_auth_codes", "oauth_clients", "oauth_personal_access_clients",
        "oauth_refresh_tokens", "mobile_otps", "mobile_sessions", "device_tokens", "tbl_credentials",
        "tbl_payment_cards", "tbl_payment_bank", "tbl_stripe_webhook", "tbl_stripe_webhook_errors",
        "migrations", "jobs", "failed_jobs", "laravel_logs", "error_logs", "mobile_api_error_logs",
        "android_app_config", "settings", "tbl_email_logs", "tbl_system_email_logs", "tbl_comm_content_logs",
        "reminder_email_logs", "push_notifications", "mobile_analytics", "deleted_users", "tbl_cms",
        "tbl_vehicle_categories_old", "tbl_lose_affiliates", "payment_logs", "stripe_transfers",
    }
)  # fmt: skip
SENSITIVE_COLUMN = re.compile(
    r"password|passwd|otp|token|secret|api_?key|hash|cvv|cvc|card_?(number|no)\b|account_?number|routing|"
    r"ssn|raw_stripe_data|charge_object|licen[cs]e_number|background_check",
    re.IGNORECASE,
)

_CUSTOMER_BOOKING_COLUMNS = (
    "id", "conf_id", "service_type", "transfer_type", "reservation_type", "pickup", "pickup_address",
    "pickup_airport", "pickup_airline", "pickup_flight", "dropoff", "dropoff_address", "dropoff_airport",
    "dropoff_airline", "dropoff_flight", "extra_stops", "pickup_date", "pickup_time", "return_pickup_date",
    "return_pickup_time", "return_pickup", "return_dropoff", "total_passengers", "luggage_count",
    "vehicle_type", "number_of_hours", "number_of_vehicles", "distance", "duration", "booking_status",
    "payment_status", "cancellation_reason", "status", "created_at", "updated_at",
)  # fmt: skip
_RIDER_BOOKING_COLUMNS = (*_CUSTOMER_BOOKING_COLUMNS, "quote_amount", "charged_amount")
_DRIVER_BOOKING_COLUMNS = (
    *_CUSTOMER_BOOKING_COLUMNS, "driver_id", "affiliate_id", "vehicle_id", "affiliate_charged_amount",
)  # fmt: skip
_VEHICLE_CATEGORY_COLUMNS = ("id", "vehicle_cat_name", "seats", "luggage", "status")


@dataclass(frozen=True, slots=True)
class TableAccess:
    """Visible columns (None = all non-sensitive) and the row filter (None = all rows)."""

    columns: tuple[str, ...] | None = None
    scope: exp.Condition | None = None


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    """What one requester may query. ``tables`` None means every non-denied table."""

    full_access: bool
    tables: Mapping[str, TableAccess] | None
    audience: str  # one line for the prompt describing whose data this is
    scoped: bool = False  # rows are pre-filtered to the requester

    def access(self, table: str) -> TableAccess | None:
        if table in DENIED_TABLES:
            return None
        if self.tables is None:
            return TableAccess()
        return self.tables.get(table)

    def visible_columns(self, table: str, live_columns: Sequence[str]) -> list[str]:
        """Columns of ``table`` this requester can see, in table order. Empty = no access."""
        access = self.access(table)
        if access is None:
            return []
        allowed = set(access.columns) if access.columns is not None else None
        return [
            c for c in live_columns if not SENSITIVE_COLUMN.search(c) and (allowed is None or c in allowed)
        ]


def _ids_in(column: str, ids: Sequence[int]) -> exp.Condition:
    if not ids:
        return exp.false()
    return exp.column(column).isin(*(exp.Literal.number(int(i)) for i in ids))


def _bookings_where(condition: exp.Condition) -> exp.Condition:
    """``reservation_id IN (SELECT id FROM tbl_reservations WHERE condition)``."""
    return exp.column("reservation_id").isin(
        query=exp.select("id").from_("tbl_reservations").where(condition.copy())
    )


def rider_policy(account_ids: Sequence[int]) -> AccessPolicy:
    own = _ids_in("acc_id", account_ids)
    return AccessPolicy(
        full_access=False,
        tables={
            "tbl_reservations": TableAccess(_RIDER_BOOKING_COLUMNS, own),
            "tbl_invoices": TableAccess(
                ("id", "reservation_id", "status", "created_at"), _bookings_where(own)
            ),
            "tbl_order": TableAccess(
                ("id", "reservation_id", "sub_total", "grand_total", "currency", "created_at"),
                _bookings_where(own),
            ),
            "tbl_passengers": TableAccess(
                ("id", "reservation_id", "passenger_name", "created_at"), _bookings_where(own)
            ),
            "tbl_vehicle_categories": TableAccess(_VEHICLE_CATEGORY_COLUMNS),
        },
        audience="the signed-in rider. Every table already contains only their own bookings and records",
        scoped=True,
    )


def driver_policy(account_ids: Sequence[int], driver_ids: Sequence[int]) -> AccessPolicy:
    own = exp.or_(_ids_in("driver_id", driver_ids), _ids_in("affiliate_id", account_ids))
    return AccessPolicy(
        full_access=False,
        tables={
            "tbl_reservations": TableAccess(_DRIVER_BOOKING_COLUMNS, own),
            "tbl_vehicles": TableAccess(
                (
                    "id",
                    "vehicle_type",
                    "year",
                    "color",
                    "license_plate",
                    "seats",
                    "luggage",
                    "status",
                    "created_at",
                ),
                _ids_in("affiliate_id", account_ids),
            ),
            "tbl_driver_feedback": TableAccess(
                ("id", "booking_id", "rating", "feedback", "created_at"), _ids_in("driver_id", driver_ids)
            ),
            "tbl_vehicle_categories": TableAccess(_VEHICLE_CATEGORY_COLUMNS),
        },
        audience=(
            "the signed-in driver/affiliate. Every table already contains only the trips assigned to them "
            "or their company, their own vehicles and their own ratings"
        ),
        scoped=True,
    )


ADMIN_POLICY = AccessPolicy(
    full_access=True, tables=None, audience="a platform administrator, with read access to all business data"
)

_ACCOUNT_IDS = text("SELECT id FROM tbl_account WHERE user_id = :user_id")
_DRIVER_IDS = text("SELECT id FROM tbl_driver_setting WHERE acc_id IN :account_ids").bindparams(
    bindparam("account_ids", expanding=True)
)


class PolicyResolver:
    """Builds a requester's policy, caching the id lookups for ``ttl_seconds``."""

    def __init__(self, engine: AsyncEngine, *, ttl_seconds: float = 60.0) -> None:
        self._engine = engine
        self._ttl = ttl_seconds
        self._cache: dict[int, tuple[float, AccessPolicy]] = {}

    @staticmethod
    def supports(requester: Requester) -> bool:
        """Whether this requester's role can query data at all (no DB access needed)."""
        return not requester.is_guest and requester.role in DATA_ROLES

    async def policy_for(self, requester: Requester) -> AccessPolicy | None:
        if not self.supports(requester) or requester.user_id is None:
            return None
        if requester.role in FULL_ACCESS_ROLES:
            return ADMIN_POLICY
        cached = self._cache.get(requester.user_id)
        if cached is not None and time.monotonic() - cached[0] < self._ttl:
            return cached[1]
        policy = await self._lookup(requester.role, requester.user_id)
        if len(self._cache) > 10_000:
            self._cache.clear()
        self._cache[requester.user_id] = (time.monotonic(), policy)
        return policy

    async def _lookup(self, role: str, user_id: int) -> AccessPolicy:
        async with self._engine.connect() as conn:
            account_ids = list((await conn.execute(_ACCOUNT_IDS, {"user_id": user_id})).scalars())
            if role in RIDER_ROLES:
                return rider_policy(account_ids)
            driver_ids = (
                list((await conn.execute(_DRIVER_IDS, {"account_ids": account_ids})).scalars())
                if account_ids
                else []
            )
        return driver_policy(account_ids, driver_ids)
