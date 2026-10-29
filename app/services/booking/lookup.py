"""Read-only booking lookups the edit flow needs and the 1800limo API doesn't offer.

- ``upcoming``: the bookings a user can pick from when they say "edit my booking" without a
  number. Scoped like the web portals: admins see every booking, riders and travel agents the
  bookings on their own account (``tbl_reservations.acc_id``). The edit API checks ownership
  again, so this only decides what is listed.
- ``related``: the other leg of a round trip (legs are separate rows linked by ``related_to``),
  so moving one leg past the other can be warned about.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine

# Bookings the edit flow refuses (EDIT_BOOKING_FLOW.md §7 Q3), with the words used to say why.
LOCKED_STATUSES = {
    "paid": "already paid",
    "paid_cash": "already paid",
    "partial_paid": "already paid",
    "cancelled": "cancelled",
    "cancellation_requested": "waiting for its cancellation",
    "ended": "completed",
    "passenger_drop": "completed",
    "finalized": "completed and closed",
    "comped": "completed",
    "refund": "refunded",
    "partial_refund": "refunded",
    "non_payable": "closed",
    "start_ride": "under way",
    "en_route_pu": "under way",
    "on_location": "under way",
    "passenger_pick": "under way",
    "en_route_do": "under way",
}

_PICKUP = "COALESCE(NULLIF(r.pickup, ''), r.pickup_address)"
_DROPOFF = "COALESCE(NULLIF(r.dropoff, ''), r.dropoff_address)"
_UPCOMING = """
SELECT r.id, r.pickup_date, r.pickup_time, {pickup} AS pickup, {dropoff} AS dropoff, r.booking_status
FROM tbl_reservations r
WHERE r.status = 'enable' AND r.pickup_date >= :today AND r.booking_status NOT IN :locked {scope}
ORDER BY r.pickup_date, r.pickup_time, r.id
LIMIT :limit
"""
_OWN = "AND r.acc_id IN (SELECT id FROM tbl_account WHERE user_id = :user_id)"
_RELATED = text(
    "SELECT r.id, r.pickup_date, r.pickup_time, (r.id = :related_to) AS is_outbound "
    "FROM tbl_reservations r "
    "WHERE r.status = 'enable' AND (r.related_to = :id OR r.id = :related_to) AND r.id <> :id"
)


@dataclass(frozen=True, slots=True)
class BookingRow:
    id: int
    pickup_at: datetime | None
    pickup: str = ""
    dropoff: str = ""
    status: str = ""


@dataclass(frozen=True, slots=True)
class RelatedLeg:
    id: int
    pickup_at: datetime | None
    is_outbound: bool  # the leg being edited is this booking's return


class ReservationLookup:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upcoming(self, mode: str, user_id: int, today: date, limit: int = 10) -> list[BookingRow]:
        """``mode`` is the booking portal (admin | individual | travel_agent)."""
        sql = text(
            _UPCOMING.format(pickup=_PICKUP, dropoff=_DROPOFF, scope="" if mode == "admin" else _OWN)
        ).bindparams(bindparam("locked", expanding=True))
        params: dict[str, Any] = {"today": today, "locked": list(LOCKED_STATUSES), "limit": limit}
        if mode != "admin":
            params["user_id"] = user_id
        async with self._engine.connect() as conn:
            rows = (await conn.execute(sql, params)).mappings().all()
        return [
            BookingRow(
                id=int(r["id"]),
                pickup_at=_when(r["pickup_date"], r["pickup_time"]),
                pickup=str(r["pickup"] or ""),
                dropoff=str(r["dropoff"] or ""),
                status=str(r["booking_status"] or ""),
            )
            for r in rows
        ]

    async def related(self, reservation_id: int, related_to: int | None) -> list[RelatedLeg]:
        params = {"id": reservation_id, "related_to": related_to or 0}
        async with self._engine.connect() as conn:
            rows = (await conn.execute(_RELATED, params)).mappings().all()
        return [
            RelatedLeg(int(r["id"]), _when(r["pickup_date"], r["pickup_time"]), bool(r["is_outbound"]))
            for r in rows
        ]


def _when(day: Any, at: Any) -> datetime | None:
    if not isinstance(day, date):
        return None
    if isinstance(at, timedelta):  # MySQL TIME comes back as a timedelta
        at = (datetime.min + at).time()
    return datetime.combine(day, at if isinstance(at, time) else time())
