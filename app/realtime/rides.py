"""Who belongs to a ride, and the coordinates proximity alerts need.

Mapping (same as Laravel's ``UserController::getActiveRide``):

* driver: ``tbl_account.user_id`` of the account in ``tbl_reservations.affiliate_id``
* rider:  ``tbl_account.user_id`` of ``acc_id``, and of ``travel_client_id`` when a travel
  agent booked for the passenger
* admin:  any ride

The result is cached in Redis for RT_RIDE_AUTH_TTL_SECONDS and dropped on every status
change or ride event, so reassignment takes effect immediately.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import orjson
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth.models import CurrentUser
from app.realtime import statuses
from app.realtime.store import RtStore
from app.realtime.tables import accounts, reservations

RideRole = Literal["driver", "rider", "admin", "viewer"]


@dataclass(frozen=True, slots=True)
class Point:
    lat: float
    lng: float


@dataclass(frozen=True, slots=True)
class RideInfo:
    id: int
    status: str
    driver_user_id: int | None
    rider_user_ids: tuple[int, ...]
    driver_first_name: str | None = None
    pickup: Point | None = None
    dropoff: Point | None = None
    stops: tuple[Point, ...] = field(default_factory=tuple)
    current_stop_index: int | None = None

    @property
    def is_live(self) -> bool:
        return self.status in statuses.LIVE_STATUSES

    @property
    def is_over(self) -> bool:
        return self.status in statuses.TERMINAL_STATUSES

    def to_cache(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_cache(cls, raw: dict[str, Any]) -> RideInfo:
        def point(value: dict[str, float] | None) -> Point | None:
            return Point(**value) if value else None

        return cls(
            id=raw["id"],
            status=raw["status"],
            driver_user_id=raw["driver_user_id"],
            rider_user_ids=tuple(raw["rider_user_ids"]),
            driver_first_name=raw.get("driver_first_name"),
            pickup=point(raw.get("pickup")),
            dropoff=point(raw.get("dropoff")),
            stops=tuple(Point(**p) for p in raw.get("stops") or ()),
            current_stop_index=raw.get("current_stop_index"),
        )

    def role_of(self, user: CurrentUser) -> RideRole | None:
        if user.role == "admin":
            return "admin"
        if user.role == "driver" and user.id == self.driver_user_id:
            return "driver"
        if user.role == "rider" and user.id in self.rider_user_ids:
            return "rider"
        return None


def _point(lat: object, lng: object) -> Point | None:
    try:
        flat, flng = float(lat), float(lng)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if flat == 0.0 or flng == 0.0 or not (-90 <= flat <= 90 and -180 <= flng <= 180):
        return None
    return Point(flat, flng)


def parse_stops(raw: object) -> tuple[Point, ...]:
    """``extra_stops`` JSON -> points. Stops without usable coordinates are skipped."""
    if isinstance(raw, str | bytes):
        try:
            raw = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return ()
    if not isinstance(raw, list):
        return ()
    out: list[Point] = []
    for stop in raw:
        if isinstance(stop, dict):
            lng = stop.get("longitude", stop.get("lng", stop.get("lon")))
            p = _point(stop.get("latitude", stop.get("lat")), lng)
            if p is not None:
                out.append(p)
    return tuple(out)


def _address(primary: object, fallback: object) -> str | None:
    for value in (primary, fallback):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def stop_summaries(raw: object) -> list[dict[str, Any]]:
    """``extra_stops`` JSON -> the stops a viewer sees, numbered in booking order. Unlike
    :func:`parse_stops`, a stop without coordinates is kept (its address is still useful)."""
    if isinstance(raw, str | bytes):
        try:
            raw = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for number, stop in enumerate(raw, start=1):
        if not isinstance(stop, dict):
            continue
        point = _point(
            stop.get("latitude", stop.get("lat")), stop.get("longitude", stop.get("lng", stop.get("lon")))
        )
        out.append(
            {
                "number": number,
                "address": _address(stop.get("address"), stop.get("location")),
                "lat": point.lat if point else None,
                "lng": point.lng if point else None,
            }
        )
    return out


class RideDirectory:
    def __init__(self, engine: AsyncEngine, store: RtStore, *, cache_ttl: int = 60) -> None:
        self._engine = engine
        self._store = store
        self._ttl = cache_ttl

    async def get(self, ride_id: int, *, fresh: bool = False) -> RideInfo | None:
        if not fresh:
            cached = await self._store.get_ride_cache(ride_id)
            if cached is not None:
                return RideInfo.from_cache(cached)
        info = await self._load(ride_id)
        if info is not None:
            await self._store.put_ride_cache(ride_id, info.to_cache(), self._ttl)
        return info

    async def summary(self, ride_id: int) -> dict[str, Any] | None:
        """What a share-link viewer needs to draw the ride: endpoints, stops, the driver's first
        name and the status. Nothing about the passenger, and no phone numbers."""
        info = await self.get(ride_id)
        if info is None:
            return None
        r = reservations
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        r.c.pickup, r.c.pickup_address, r.c.dropoff, r.c.dropoff_address, r.c.extra_stops
                    ).where(r.c.id == ride_id)
                )
            ).first()
        if row is None:
            return None
        m = row._mapping

        def end(address: str | None, point: Point | None) -> dict[str, Any]:
            return {
                "address": address,
                "lat": point.lat if point else None,
                "lng": point.lng if point else None,
            }

        return {
            "ride_id": str(info.id),
            "status": info.status,
            "driver_first_name": info.driver_first_name,
            "pickup": end(_address(m["pickup"], m["pickup_address"]), info.pickup),
            "dropoff": end(_address(m["dropoff"], m["dropoff_address"]), info.dropoff),
            "stops": stop_summaries(m["extra_stops"]),
            "current_stop_index": info.current_stop_index,
        }

    async def invalidate(self, ride_id: int) -> None:
        await self._store.drop_ride_cache(ride_id)

    async def _load(self, ride_id: int) -> RideInfo | None:
        rider_acc = accounts.alias("rider_acc")
        client_acc = accounts.alias("client_acc")
        driver_acc = accounts.alias("driver_acc")
        r = reservations
        query = (
            select(
                r.c.id,
                r.c.booking_status,
                r.c.service_type,
                r.c.extra_stops,
                r.c.pickup_latitude,
                r.c.pickup_longitude,
                r.c.dropoff_latitude,
                r.c.dropoff_longitude,
                r.c.current_stop_index,
                rider_acc.c.user_id.label("rider_user_id"),
                client_acc.c.user_id.label("client_user_id"),
                driver_acc.c.user_id.label("driver_user_id"),
                driver_acc.c.first_name.label("driver_first_name"),
            )
            .select_from(
                r.outerjoin(rider_acc, rider_acc.c.id == r.c.acc_id)
                .outerjoin(client_acc, client_acc.c.id == r.c.travel_client_id)
                .outerjoin(driver_acc, driver_acc.c.id == r.c.affiliate_id)
            )
            .where(r.c.id == ride_id)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:
            return None
        m = row._mapping
        riders = tuple(dict.fromkeys(uid for uid in (m["rider_user_id"], m["client_user_id"]) if uid))
        return RideInfo(
            id=int(m["id"]),
            status=str(m["booking_status"] or ""),
            driver_user_id=int(m["driver_user_id"]) if m["driver_user_id"] else None,
            rider_user_ids=tuple(int(u) for u in riders),
            driver_first_name=m["driver_first_name"],
            pickup=_point(m["pickup_latitude"], m["pickup_longitude"]),
            dropoff=_point(m["dropoff_latitude"], m["dropoff_longitude"]),
            stops=parse_stops(m["extra_stops"]),
            current_stop_index=m["current_stop_index"],
        )

    async def active_ride_for(self, user: CurrentUser) -> tuple[int, str] | None:
        """The user's live ride (newest first), as Laravel's active-ride endpoint picks it."""
        if user.role not in ("driver", "rider"):
            return None
        r, a = reservations, accounts
        if user.role == "driver":
            join_on = a.c.id == r.c.affiliate_id
        else:
            join_on = or_(a.c.id == r.c.acc_id, a.c.id == r.c.travel_client_id)
        query = (
            select(r.c.id, r.c.booking_status)
            .select_from(r.join(a, join_on))
            .where(a.c.user_id == user.id, r.c.booking_status.in_(sorted(statuses.LIVE_STATUSES)))
            .order_by(r.c.updated_at.desc(), r.c.id.desc())
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return (int(row[0]), str(row[1])) if row is not None else None
