"""Ride status writes, ported from Laravel's ``BookingTrackingService::recordStatusUpdate``.

Same rules, in one transaction with the reservation row locked:

1. reject statuses outside the vocabulary;
2. ignore duplicates: same idempotency key, or same (status, stop index, event time)
   within 30 minutes;
3. an update older than the newest recorded event is "stale": history only;
4. otherwise update ``booking_status``, ``status_change_flag``, ``updated_at`` and the
   charter stop columns, with the same reset rules;
5. always append a ``tbl_booking_status_history`` row (in a savepoint: losing the audit
   row never fails the status write, as in Laravel);
6. report whether this was the real transition into ``ended`` (the ride-completed SMS).

Side effects outside the database (broadcast, push, SMS) are the caller's job, after commit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import orjson
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.logging import get_logger
from app.realtime import statuses
from app.realtime.db import utcnow
from app.realtime.tables import reservations, status_history

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StatusContext:
    stop_info: dict[str, Any] | None = None
    timestamp: Any = None
    driver_user_id: int | None = None
    customer_user_id: int | None = None
    location: dict[str, Any] | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = None
    source: str = "rt"
    reason: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class StatusResult:
    found: bool
    updated: bool = False
    duplicate: bool = False
    stale: bool = False
    message: str | None = None
    previous_status: str | None = None
    status: str | None = None  # what the booking holds after this call
    stop_info: dict[str, Any] | None = None
    current_stop_index: int | None = None
    total_stops: int | None = None
    event_at: datetime | None = None
    completed_transition: bool = False  # real move into "ended": send the thank-you SMS

    @property
    def accepted(self) -> bool:
        return self.updated or self.duplicate or self.stale


def parse_event_time(value: Any) -> datetime | None:
    """Client event time -> naive UTC, whole seconds (the column is a MySQL TIMESTAMP).

    Accepts epoch seconds or milliseconds (number or digit string) and ISO 8601.
    Zone-less ISO strings are taken as UTC.
    """
    if value is None or value == "":
        return None
    try:
        if isinstance(value, int | float) or (isinstance(value, str) and value.strip().isdigit()):
            number = float(value)
            if not math.isfinite(number) or number <= 0:
                return None
            seconds = number / 1000 if number > 1e11 else number
            parsed = datetime.fromtimestamp(seconds, UTC)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(UTC)
        else:
            return None
    except (ValueError, OverflowError, OSError):
        logger.warning("rt_status_bad_timestamp")
        return None
    return parsed.replace(tzinfo=None, microsecond=0)


def _coordinate(value: Any) -> float | None:
    """Coordinates are omitted (not zeroed) when the app could not resolve the stop."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number == 0.0 or not math.isfinite(number) else number


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return math.isfinite(value)
    if isinstance(value, str):
        try:
            float(value)
        except ValueError:
            return False
        return True
    return False


def extra_stops(raw: Any) -> list[Any]:
    """``tbl_reservations.extra_stops`` decoded like Laravel's getExtraStops()."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        decoded = orjson.loads(raw)
    except (orjson.JSONDecodeError, TypeError):
        return []
    return decoded if isinstance(decoded, list) else []


def normalize_stop_info(ride_id: int, raw_stops: Any, status: str, stop_info: Any) -> dict[str, Any] | None:
    if status not in statuses.STOP_STATUSES:
        return None
    if not isinstance(stop_info, dict) or not _is_number(stop_info.get("index")):
        logger.warning("rt_stop_status_without_stop_info", ride_id=ride_id, status=status)
        return None
    index = int(float(stop_info["index"]))
    if index < 0:
        return None
    stops = extra_stops(raw_stops)
    total = int(float(stop_info["total_stops"])) if _is_number(stop_info.get("total_stops")) else len(stops)
    if stops and index >= len(stops):
        # Booking edited mid-tour: keep the app's numbers, it is what the driver acted on.
        logger.warning("rt_stop_index_out_of_range", ride_id=ride_id, stop_index=index, stops=len(stops))
    address = stop_info.get("address")
    return {
        "index": index,
        "stop_number": int(float(stop_info["stop_number"]))
        if _is_number(stop_info.get("stop_number"))
        else index + 1,
        "total_stops": total,
        "address": str(address)[:512] if address is not None else None,
        "latitude": _coordinate(stop_info.get("latitude")),
        "longitude": _coordinate(stop_info.get("longitude")),
    }


class StatusWriter:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, ride_id: int, new_status: str, ctx: StatusContext) -> StatusResult:
        if new_status not in statuses.ALL_STATUSES:
            return StatusResult(found=True, message=f"Invalid booking status: {new_status}")

        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(
                        reservations.c.id,
                        reservations.c.booking_status,
                        reservations.c.extra_stops,
                        reservations.c.current_stop_index,
                        reservations.c.total_stops,
                    )
                    .where(reservations.c.id == ride_id)
                    .with_for_update()
                )
            ).first()
            if row is None:
                return StatusResult(found=False, message="Booking not found")

            previous = row.booking_status
            stop_info = normalize_stop_info(ride_id, row.extra_stops, new_status, ctx.stop_info)
            event_at = parse_event_time(ctx.timestamp)

            if await self._is_duplicate(conn, ride_id, new_status, stop_info, ctx, event_at):
                logger.info(
                    "rt_status_duplicate",
                    ride_id=ride_id,
                    status=new_status,
                    stop_index=stop_info["index"] if stop_info else None,
                )
                return StatusResult(
                    found=True,
                    duplicate=True,
                    message="Duplicate status update ignored",
                    previous_status=previous,
                    status=previous,
                    stop_info=stop_info,
                    current_stop_index=row.current_stop_index,
                    total_stops=row.total_stops,
                    event_at=event_at,
                )

            stale = await self._is_stale(conn, ride_id, event_at)
            current_index, total = row.current_stop_index, row.total_stops
            stored = previous
            if not stale:
                now = utcnow()
                update: dict[str, Any] = {
                    "booking_status": new_status,
                    "status_change_flag": "yes",
                    "updated_at": now,
                }
                if stop_info is not None:
                    update |= {
                        "current_stop_index": stop_info["index"],
                        "current_stop_status": new_status,
                        "total_stops": stop_info["total_stops"],
                        "stop_status_updated_at": event_at or now,
                    }
                elif new_status == statuses.START_RIDE:
                    # The tour has begun but no stop has been served yet.
                    update |= {
                        "current_stop_index": None,
                        "current_stop_status": None,
                        "total_stops": len(extra_stops(row.extra_stops)),
                        "stop_status_updated_at": event_at or now,
                    }
                elif new_status in (statuses.EN_ROUTE_PU, statuses.ON_LOCATION):
                    # Back on the pickup leg: a stop cursor left from an earlier run is stale.
                    update |= {
                        "current_stop_index": None,
                        "current_stop_status": None,
                        "stop_status_updated_at": None,
                    }
                await conn.execute(reservations.update().where(reservations.c.id == ride_id).values(**update))
                stored = new_status
                current_index = update.get("current_stop_index", current_index)
                total = update.get("total_stops", total)

            await self._write_history(conn, ride_id, previous, new_status, stop_info, event_at, ctx)

        completed = (
            not stale
            and new_status == statuses.ENDED
            and previous not in (statuses.ENDED, statuses.FINALIZED)
        )
        logger.info(
            "rt_status_recorded",
            ride_id=ride_id,
            previous_status=previous,
            new_status=new_status,
            stop_index=stop_info["index"] if stop_info else None,
            stale=stale,
            source=ctx.source,
            correlation_id=ctx.correlation_id,
        )
        return StatusResult(
            found=True,
            updated=not stale,
            stale=stale,
            message="Status update arrived out of order; recorded in history only" if stale else None,
            previous_status=previous,
            status=stored,
            stop_info=stop_info,
            current_stop_index=current_index,
            total_stops=total,
            event_at=event_at,
            completed_transition=completed,
        )

    async def _is_duplicate(
        self,
        conn: AsyncConnection,
        ride_id: int,
        status: str,
        stop_info: dict[str, Any] | None,
        ctx: StatusContext,
        event_at: datetime | None,
    ) -> bool:
        h = status_history.c
        if ctx.idempotency_key:
            hit = await conn.scalar(select(h.id).where(h.idempotency_key == ctx.idempotency_key).limit(1))
            if hit is not None:
                return True
        if event_at is None:
            # Nothing tells a retry from a genuine repeat; Laravel lets these through too.
            return False
        query = select(h.id).where(
            h.reservation_id == ride_id,
            h.booking_status == status,
            h.event_at == event_at,
            h.created_at >= utcnow() - timedelta(minutes=statuses.DEDUP_WINDOW_MINUTES),
        )
        query = query.where(
            h.stop_index.is_(None) if stop_info is None else h.stop_index == stop_info["index"]
        )
        return await conn.scalar(query.limit(1)) is not None

    async def _is_stale(self, conn: AsyncConnection, ride_id: int, event_at: datetime | None) -> bool:
        if event_at is None:
            return False
        h = status_history.c
        latest = await conn.scalar(
            select(func.max(h.event_at)).where(h.reservation_id == ride_id, h.event_at.is_not(None))
        )
        if latest is None:
            return False
        if isinstance(latest, str):  # SQLite in tests returns text
            latest = datetime.fromisoformat(latest)
        return event_at < latest

    async def _write_history(
        self,
        conn: AsyncConnection,
        ride_id: int,
        previous: str | None,
        status: str,
        stop_info: dict[str, Any] | None,
        event_at: datetime | None,
        ctx: StatusContext,
    ) -> None:
        location = ctx.location if isinstance(ctx.location, dict) else {}
        now = utcnow()
        values = {
            "reservation_id": ride_id,
            "previous_status": previous,
            "booking_status": status,
            "stop_index": stop_info["index"] if stop_info else None,
            "stop_number": stop_info["stop_number"] if stop_info else None,
            "total_stops": stop_info["total_stops"] if stop_info else None,
            "stop_address": stop_info["address"] if stop_info else None,
            "stop_latitude": stop_info["latitude"] if stop_info else None,
            "stop_longitude": stop_info["longitude"] if stop_info else None,
            "driver_id": ctx.driver_user_id,
            "customer_id": ctx.customer_user_id,
            "latitude": _coordinate(location.get("latitude", location.get("lat"))),
            "longitude": _coordinate(location.get("longitude", location.get("lng"))),
            "event_at": event_at,
            "source": ctx.source[:32],
            "correlation_id": (ctx.correlation_id or None) and ctx.correlation_id[:128],
            "idempotency_key": (ctx.idempotency_key or None) and ctx.idempotency_key[:128],
            "metadata": ctx.metadata,
            "created_at": now,
            "updated_at": now,
        }
        try:
            async with conn.begin_nested():
                await conn.execute(status_history.insert().values(**values))
        except SQLAlchemyError as exc:
            logger.error("rt_status_history_failed", ride_id=ride_id, status=status, error=type(exc).__name__)
