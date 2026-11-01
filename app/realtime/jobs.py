"""Periodic jobs. Each tick takes a Redis lock, so a second limo-rt instance never runs
the same job twice (no double pushes, no duplicate trail rows).

* trail flush (every RT_TRAIL_FLUSH_SECONDS): sampled points -> tbl_rt_location_points
* proximity (every RT_PROXIMITY_INTERVAL_SECONDS): 1 mi and 1/4 mi alerts, once per leg
* retention (daily, off until RT_RETENTION_ENABLED): trail points older than N months
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.logging import get_logger
from app.realtime import statuses
from app.realtime.db import utcnow
from app.realtime.geo import METERS_PER_MILE, haversine_m
from app.realtime.location import ACTIVE_RIDES_KEY
from app.realtime.rides import Point, RideInfo
from app.realtime.services import Realtime
from app.realtime.tables import location_points
from app.realtime.watcher import RideWatcher

logger = get_logger(__name__)

FLUSH_BATCH = 500
ONE_MILE_M = METERS_PER_MILE
QUARTER_MILE_M = METERS_PER_MILE / 4
STALE_LOCATION_S = 120  # rides with no fix for this long drop out of the proximity scan
RETENTION_BATCH = 5000


class Jobs:
    def __init__(self, rt: Realtime) -> None:
        self._rt = rt
        self._tasks: list[asyncio.Task[None]] = []
        self.watcher = RideWatcher(rt.engine, rt.store, rt.rides, rt.events)

    def start(self) -> None:
        s = self._rt.settings
        self._tasks = [
            asyncio.create_task(self._every("trail_flush", s.rt_trail_flush_seconds, self.flush_trail)),
            asyncio.create_task(self._every("proximity", s.rt_proximity_interval_seconds, self.proximity)),
            asyncio.create_task(self._every("watch", s.rt_watch_interval_seconds, self.watcher.scan_recent)),
            asyncio.create_task(self._every("sweep", s.rt_watch_sweep_seconds, self.watcher.sweep_open)),
        ]
        if s.rt_retention_enabled:
            self._tasks.append(asyncio.create_task(self._every("retention", 86400, self.retention)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(Exception):
            await self.flush_trail()  # don't strand sampled points across a deploy

    async def _every(self, name: str, seconds: float, job: Callable[[], Awaitable[Any]]) -> None:
        lock_key = f"rt:lock:{name}"
        while True:
            await asyncio.sleep(seconds)
            try:
                if await self._rt.redis.set(lock_key, 1, nx=True, ex=max(1, int(seconds))):
                    await job()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("rt_job_failed", job=name, error=repr(exc))

    # --- trail -------------------------------------------------------------------------

    async def flush_trail(self) -> int:
        total = 0
        while True:
            points = await self._rt.store.pop_trail_points(FLUSH_BATCH)
            if not points:
                return total
            now = utcnow()
            rows = [
                {
                    "reservation_id": p["reservation_id"],
                    "driver_user_id": p.get("driver_user_id"),
                    "recorded_at": datetime.fromtimestamp(p["recorded_at_ms"] / 1000, UTC).replace(
                        tzinfo=None
                    ),
                    "lat": round(p["lat"], 7),
                    "lng": round(p["lng"], 7),
                    "accuracy_m": p.get("accuracy_m"),
                    "speed_mps": p.get("speed_mps"),
                    "heading": p.get("heading"),
                    "source": p.get("source"),
                    "created_at": now,
                }
                for p in points
            ]
            try:
                async with self._rt.engine.begin() as conn:
                    await conn.execute(location_points.insert(), rows)
            except SQLAlchemyError as exc:
                await self._rt.store.requeue_trail_points(points)
                logger.error("rt_trail_flush_failed", points=len(points), error=type(exc).__name__)
                return total
            total += len(rows)
            if len(points) < FLUSH_BATCH:
                return total

    # --- proximity ---------------------------------------------------------------------

    async def proximity(self) -> int:
        if not self._rt.flags.enabled("rt.proximity_alerts"):
            return 0
        redis = self._rt.redis
        await redis.zremrangebyscore(ACTIVE_RIDES_KEY, "-inf", time.time() - STALE_LOCATION_S)
        sent = 0
        for raw_id in await redis.zrange(ACTIVE_RIDES_KEY, 0, -1):
            ride_id = int(raw_id)
            info = await self._rt.rides.get(ride_id)
            latest = await self._rt.store.get_location(ride_id)
            if info is None or latest is None or not info.is_live:
                continue
            sent += await self._check_ride(info, latest["data"])
        return sent

    async def _check_ride(self, info: RideInfo, fix: dict[str, Any]) -> int:
        target = next_target(info)
        if target is None:
            return 0
        leg, point = target
        meters = haversine_m(fix["lat"], fix["lng"], point.lat, point.lng)
        store = self._rt.store
        if meters <= QUARTER_MILE_M:
            await store.once(info.id, f"alert:{leg}:1mi")  # never send "a mile away" after this
            if await store.once(info.id, f"alert:{leg}:qmi"):
                await self._rt.events.approaching(info, leg=leg, threshold="qmi", meters=round(meters))
                return 1
        elif meters <= ONE_MILE_M and await store.once(info.id, f"alert:{leg}:1mi"):
            await self._rt.events.approaching(info, leg=leg, threshold="1mi", meters=round(meters))
            return 1
        return 0

    # --- retention ---------------------------------------------------------------------

    async def retention(self) -> int:
        cutoff = utcnow() - timedelta(days=30 * self._rt.settings.rt_retention_months)
        deleted = 0
        while True:
            async with self._rt.engine.begin() as conn:
                ids = (
                    (
                        await conn.execute(
                            select(location_points.c.id)
                            .where(location_points.c.recorded_at < cutoff)
                            .limit(RETENTION_BATCH)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not ids:
                    break
                await conn.execute(delete(location_points).where(location_points.c.id.in_(ids)))
            deleted += len(ids)
        if deleted:
            logger.info("rt_retention_deleted", rows=deleted, cutoff=cutoff.isoformat())
        return deleted


def next_target(info: RideInfo) -> tuple[str, Point] | None:
    """Where the driver is heading now: (leg name, point)."""
    status = info.status
    if status == statuses.EN_ROUTE_PU:
        return ("pickup", info.pickup) if info.pickup else None
    if status in (statuses.START_RIDE, statuses.PASSENGER_PICK, statuses.EN_ROUTE_DO):
        if status == statuses.EN_ROUTE_DO or not info.stops:
            return ("dropoff", info.dropoff) if info.dropoff else None
        index = (
            0
            if status == statuses.START_RIDE or info.current_stop_index is None
            else info.current_stop_index + 1
        )
        if index < len(info.stops):
            return (f"stop{index}", info.stops[index])
        return ("dropoff", info.dropoff) if info.dropoff else None
    return None
