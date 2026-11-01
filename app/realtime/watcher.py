"""Notice ride changes Laravel makes, wherever they come from.

Bookings are cancelled, reassigned and edited from ~30 code paths (admin, affiliate
portal, travel planner, quote bot, IVR, driver API, crons), several of them plain
query-builder updates. Instead of adding a call to each, limo-rt watches
``tbl_reservations``:

* every RT_WATCH_INTERVAL_SECONDS: rows whose ``updated_at`` moved since the last scan;
* every RT_WATCH_SWEEP_SECONDS: every open ride, for writes that didn't touch ``updated_at``.

Each row is compared with its snapshot in Redis (``rt:ride:{id}:snap``: status, affiliate,
driver). A changed driver -> ``driver_changed``; status -> cancelled -> ``ride_cancelled``;
any other status change -> ``ride_updated``. limo-rt's own status writes update the
snapshot as they commit, so they never come back as events. A ride seen for the first
time only gets a snapshot.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.realtime import statuses
from app.realtime.db import utcnow
from app.realtime.events import RideEvents
from app.realtime.rides import RideDirectory
from app.realtime.store import SNAPSHOT_TTL, RtStore
from app.realtime.tables import accounts, reservations

logger = get_logger(__name__)

WATERMARK_KEY = "rt:watch:since"
OVERLAP = timedelta(seconds=10)  # re-read a little: commits can land out of updated_at order
MAX_ROWS = 500
OPEN_STATUSES = sorted(statuses.LIVE_STATUSES | {statuses.ASSIGNED, statuses.PENDING})


class RideWatcher:
    def __init__(self, engine: Any, store: RtStore, rides: RideDirectory, events: RideEvents) -> None:
        self._engine = engine
        self._store = store
        self._rides = rides
        self._events = events

    async def scan_recent(self) -> int:
        raw = await self._store.redis.get(WATERMARK_KEY)
        since = datetime.fromisoformat(raw.decode()) if raw else utcnow() - timedelta(minutes=5)
        started = utcnow()
        rows = await self._fetch(reservations.c.updated_at >= since - OVERLAP)
        changes = await self._compare(rows)
        newest = max((r["updated_at"] for r in rows if isinstance(r["updated_at"], datetime)), default=None)
        mark = max(started - OVERLAP, newest) if newest else started - OVERLAP
        await self._store.redis.set(WATERMARK_KEY, mark.isoformat(), ex=SNAPSHOT_TTL)

        return changes

    async def sweep_open(self) -> int:
        return await self._compare(await self._fetch(reservations.c.booking_status.in_(OPEN_STATUSES)))

    async def _fetch(self, condition: Any) -> list[dict[str, Any]]:
        r = reservations.c
        query = (
            select(r.id, r.booking_status, r.affiliate_id, r.driver_id, r.updated_at)
            .where(condition)
            .order_by(r.updated_at.desc())
            .limit(MAX_ROWS)
        )
        async with self._engine.connect() as conn:
            rows = [dict(row._mapping) for row in await conn.execute(query)]
        for row in rows:
            if isinstance(row["updated_at"], str):  # SQLite in tests
                row["updated_at"] = datetime.fromisoformat(row["updated_at"])
        return rows

    async def _compare(self, rows: list[dict[str, Any]]) -> int:
        changes = 0
        for row in rows:
            ride_id = int(row["id"])
            current = {
                "status": str(row["booking_status"] or ""),
                "affiliate_id": row["affiliate_id"],
                "driver_id": row["driver_id"],
            }
            previous = await self._store.get_snapshot(ride_id)
            await self._store.put_snapshot(ride_id, current)
            if previous is None or previous == current:
                continue
            changes += 1
            await self._rides.invalidate(ride_id)
            try:
                await self._emit(ride_id, previous, current)
            except Exception as exc:  # one bad ride must not stop the scan
                logger.error("rt_watch_emit_failed", ride_id=ride_id, error=repr(exc))
        return changes

    async def _emit(self, ride_id: int, previous: dict[str, Any], current: dict[str, Any]) -> None:
        driver_moved = (previous["affiliate_id"], previous["driver_id"]) != (
            current["affiliate_id"],
            current["driver_id"],
        )
        status_moved = previous["status"] != current["status"]
        logger.info(
            "rt_watch_change",
            ride_id=ride_id,
            status=[previous["status"], current["status"]] if status_moved else None,
            driver_changed=driver_moved,
        )
        if status_moved and current["status"] == statuses.CANCELLED:
            await self._events.ride_cancelled(ride_id, by="admin")
            return
        if driver_moved:
            previous_user = await self._driver_user(previous["affiliate_id"])
            await self._events.driver_changed(ride_id, previous_driver_user_id=previous_user)
        if status_moved:
            await self._events.ride_updated(ride_id, status=current["status"])

    async def _driver_user(self, affiliate_id: Any) -> int | None:
        if not affiliate_id:
            return None
        async with self._engine.connect() as conn:
            user = await conn.scalar(select(accounts.c.user_id).where(accounts.c.id == affiliate_id))
        return int(user) if user else None
