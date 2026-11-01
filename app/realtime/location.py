"""Driver location hot path and trail sampling.

``loc.update`` from the assigned driver -> order check on (epoch, seq) -> store as the
latest location (short TTL) and publish to the ride -> maybe keep it as a trail point.
No database write per GPS fix: trail points go to a Redis list that a job flushes to
``tbl_rt_location_points`` in batches (one point per ride every 10 s or 25 m).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.auth.models import CurrentUser
from app.realtime.geo import haversine_m
from app.realtime.protocol import LocationData, ProtocolError, envelope, now_ms
from app.realtime.rides import RideDirectory
from app.realtime.store import RtStore

ACTIVE_RIDES_KEY = "rt:rides:active"
# A new app launch (new epoch) may restart seq at 0; accept it unless its fix is clearly older.
EPOCH_CHANGE_TOLERANCE_MS = 2000


@dataclass(slots=True)
class _Kept:
    at_ms: int
    lat: float
    lng: float


class TrailSampler:
    """Keeps one point per ride every ``min_seconds`` or ``min_meters``, whichever first."""

    def __init__(self, store: RtStore, *, min_seconds: float, min_meters: float) -> None:
        self._store = store
        self._min_ms = int(min_seconds * 1000)
        self._min_m = min_meters
        self._last: dict[int, _Kept] = {}

    async def offer(self, ride_id: int, driver_user_id: int, fix: LocationData) -> bool:
        last = self._last.get(ride_id)
        if last is not None:
            moved = haversine_m(last.lat, last.lng, fix.lat, fix.lng)
            if fix.gps_ts - last.at_ms < self._min_ms and moved < self._min_m:
                return False
        self._last[ride_id] = _Kept(fix.gps_ts, fix.lat, fix.lng)
        await self._store.push_trail_point(
            {
                "reservation_id": ride_id,
                "driver_user_id": driver_user_id,
                "recorded_at_ms": fix.gps_ts,
                "lat": fix.lat,
                "lng": fix.lng,
                "accuracy_m": fix.acc_m,
                "speed_mps": fix.speed_mps,
                "heading": fix.heading,
                "source": fix.src,
            }
        )
        return True

    async def status_point(self, ride_id: int, driver_user_id: int | None, lat: float, lng: float) -> None:
        """Always keep the position where a status changed."""
        await self._store.push_trail_point(
            {
                "reservation_id": ride_id,
                "driver_user_id": driver_user_id,
                "recorded_at_ms": now_ms(),
                "lat": lat,
                "lng": lng,
                "source": "status",
            }
        )

    def forget(self, ride_id: int) -> None:
        self._last.pop(ride_id, None)


class LocationIngest:
    def __init__(self, store: RtStore, rides: RideDirectory, trail: TrailSampler) -> None:
        self._store = store
        self._rides = rides
        self._trail = trail

    async def ingest(self, user: CurrentUser, ride_id: int, fix: LocationData) -> bool:
        """Returns False when the fix was dropped as out of order (not an error)."""
        info = await self._rides.get(ride_id)
        if info is None or info.role_of(user) != "driver" or info.is_over:
            raise ProtocolError("forbidden_ride")

        previous = await self._store.get_location(ride_id)
        if previous is not None and not _is_newer(previous.get("data", {}), fix):
            return False

        data: dict[str, Any] = {
            "lat": fix.lat,
            "lng": fix.lng,
            "acc_m": fix.acc_m,
            "speed_mps": fix.speed_mps,
            "heading": fix.heading,
            "gps_ts": fix.gps_ts,
            "epoch": fix.epoch,
            "seq": fix.seq,
            "src": fix.src,
            "snapshot": False,
        }
        await self._store.put_location(ride_id, envelope("loc", data, ride_id=ride_id))
        await self._store.redis.zadd(ACTIVE_RIDES_KEY, {str(ride_id): time.time()})
        await self._trail.offer(ride_id, user.id, fix)
        return True


def _is_newer(previous: dict[str, Any], fix: LocationData) -> bool:
    if previous.get("epoch") == fix.epoch:
        return fix.seq > int(previous.get("seq", -1))
    return fix.gps_ts > int(previous.get("gps_ts", 0)) - EPOCH_CHANGE_TOLERANCE_MS


def snapshot_frame(stored: dict[str, Any]) -> dict[str, Any]:
    """The stored latest location, re-sent to a (re)subscribing client."""
    frame = dict(stored)
    frame["data"] = {**stored.get("data", {}), "snapshot": True}
    return frame
