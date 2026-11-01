"""Redis state for the realtime service: event streams, latest location, presence, acks.

All realtime state lives here rather than in process memory, so a restart (every deploy)
loses nothing: clients reconnect, resubscribe with ``last_seq`` and get replayed.

Key layout (``rt:`` prefix):

    rt:ride:{id}:events     stream; entry id "<seq>-0", field "env" = envelope JSON
    rt:ride:{id}:seq        last issued seq
    rt:ride:{id}:loc        latest driver location frame (JSON), short TTL
    rt:ride:{id}:auth       cached ride access info (JSON), short TTL
    rt:ride:{id}:ack:{uid}  highest seq the user acked
    rt:ride:{id}:alert:...  proximity alert sent (SET NX)
    rt:ride:{id}:sms        SMS fallback counter
    rt:presence:{uid}       hash conn_id -> app_state, TTL refreshed by client frames
    rt:trail:pending        list of sampled trail points waiting for the MySQL flush
    channels: rt:ride:{id}, rt:user:{uid}
"""

from __future__ import annotations

from typing import Any

import orjson
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.realtime.protocol import dumps, envelope

TRAIL_PENDING_KEY = "rt:trail:pending"
CONTROL_CHANNEL = "rt:ctl"
SNAPSHOT_TTL = 14 * 86400
_MAX_SEQ_RETRIES = 5


def ride_channel(ride_id: int) -> str:
    return f"rt:ride:{ride_id}"


def user_channel(user_id: int) -> str:
    return f"rt:user:{user_id}"


def _k(ride_id: int, suffix: str) -> str:
    return f"rt:ride:{ride_id}:{suffix}"


class RtStore:
    """Thin, typed wrapper over Redis for everything realtime."""

    def __init__(
        self,
        redis: Redis,
        *,
        stream_max_entries: int = 1000,
        stream_ttl_active: int = 7 * 86400,
        stream_ttl_ended: int = 86400,
        location_ttl: int = 300,
        presence_ttl: int = 45,
    ) -> None:
        self.redis = redis
        self._stream_max = stream_max_entries
        self._ttl_active = stream_ttl_active
        self._ttl_ended = stream_ttl_ended
        self._loc_ttl = location_ttl
        self._presence_ttl = presence_ttl

    # --- Durable ride events -------------------------------------------------------------

    async def append_event(
        self, ride_id: int, type_: str, data: dict[str, Any], *, ride_over: bool = False
    ) -> dict[str, Any]:
        """Give the event the next seq, store it for replay and publish it to the ride.

        The stream entry id *is* the seq ("<seq>-0"), so stream order and seq order can
        never disagree. If two writers race, the later XADD fails and retries with a new seq.
        """
        events_key, seq_key = _k(ride_id, "events"), _k(ride_id, "seq")
        for _ in range(_MAX_SEQ_RETRIES):
            seq = int(await self.redis.incr(seq_key))
            message = envelope(type_, data, ride_id=ride_id, seq=seq)
            payload = dumps(message)
            try:
                await self.redis.xadd(
                    events_key,
                    {"env": payload},
                    id=f"{seq}-0",
                    maxlen=self._stream_max,
                    approximate=True,
                )
            except ResponseError as exc:
                if "equal or smaller" in str(exc):
                    continue
                raise
            ttl = self._ttl_ended if ride_over else self._ttl_active
            async with self.redis.pipeline(transaction=False) as pipe:
                pipe.expire(events_key, ttl)
                pipe.expire(seq_key, ttl)
                pipe.publish(ride_channel(ride_id), payload)
                await pipe.execute()
            return message
        raise RuntimeError(f"could not allocate a seq for ride {ride_id}")

    async def replay(self, ride_id: int, after_seq: int) -> tuple[list[str], bool]:
        """Frames with seq > after_seq, oldest first, and whether some were already trimmed."""
        events_key = _k(ride_id, "events")
        entries = await self.redis.xrange(events_key, min=f"{after_seq + 1}-0", max="+")
        frames = [fields[b"env"].decode() for _, fields in entries]
        gap = False
        if after_seq > 0:
            first = await self.redis.xrange(events_key, min="-", max="+", count=1)
            if first:
                first_seq = int(first[0][0].decode().split("-", 1)[0])
                gap = first_seq > after_seq + 1
            else:
                current = await self.current_seq(ride_id)
                gap = current > after_seq
        return frames, gap

    async def current_seq(self, ride_id: int) -> int:
        value = await self.redis.get(_k(ride_id, "seq"))
        return int(value) if value is not None else 0

    async def mark_ride_over(self, ride_id: int) -> None:
        async with self.redis.pipeline(transaction=False) as pipe:
            for suffix in ("events", "seq"):
                pipe.expire(_k(ride_id, suffix), self._ttl_ended)
            pipe.delete(_k(ride_id, "loc"))
            await pipe.execute()

    async def publish_user(self, user_id: int, message: dict[str, Any]) -> None:
        await self.redis.publish(user_channel(user_id), dumps(message))

    # --- Latest location -----------------------------------------------------------------

    async def get_location(self, ride_id: int) -> dict[str, Any] | None:
        raw = await self.redis.get(_k(ride_id, "loc"))
        return orjson.loads(raw) if raw is not None else None

    async def put_location(self, ride_id: int, message: dict[str, Any]) -> None:
        payload = dumps(message)
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.set(_k(ride_id, "loc"), payload, ex=self._loc_ttl)
            pipe.publish(ride_channel(ride_id), payload)
            await pipe.execute()

    # --- Ride access cache ---------------------------------------------------------------

    async def get_ride_cache(self, ride_id: int) -> dict[str, Any] | None:
        raw = await self.redis.get(_k(ride_id, "auth"))
        return orjson.loads(raw) if raw is not None else None

    async def put_ride_cache(self, ride_id: int, info: dict[str, Any], ttl: int) -> None:
        await self.redis.set(_k(ride_id, "auth"), orjson.dumps(info), ex=ttl)

    async def drop_ride_cache(self, ride_id: int) -> None:
        await self.redis.delete(_k(ride_id, "auth"))

    # --- Presence and acks ---------------------------------------------------------------

    async def touch_presence(self, user_id: int, conn_id: str, app_state: str) -> None:
        key = f"rt:presence:{user_id}"
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.hset(key, conn_id, app_state)
            pipe.expire(key, self._presence_ttl)
            await pipe.execute()

    async def drop_presence(self, user_id: int, conn_id: str) -> None:
        await self.redis.hdel(f"rt:presence:{user_id}", conn_id)

    async def presence(self, user_id: int) -> str | None:
        """'foreground' if any live connection is in the foreground, 'background', or None."""
        states = await self.redis.hvals(f"rt:presence:{user_id}")
        if not states:
            return None
        return "foreground" if b"foreground" in states else "background"

    async def record_ack(self, ride_id: int, user_id: int, seq: int) -> None:
        key = _k(ride_id, f"ack:{user_id}")
        current = await self.redis.get(key)
        if current is None or int(current) < seq:
            await self.redis.set(key, seq, ex=self._ttl_active)

    async def acked(self, ride_id: int, user_id: int) -> int:
        value = await self.redis.get(_k(ride_id, f"ack:{user_id}"))
        return int(value) if value is not None else 0

    # --- Once-only markers and counters --------------------------------------------------

    async def once(self, ride_id: int, marker: str, ttl: int = 48 * 3600) -> bool:
        """True the first time this marker is set for the ride, False afterwards."""
        return bool(await self.redis.set(_k(ride_id, marker), 1, nx=True, ex=ttl))

    async def take_sms_slot(self, ride_id: int, cap: int) -> bool:
        key = _k(ride_id, "sms")
        count = int(await self.redis.incr(key))
        await self.redis.expire(key, 48 * 3600)
        return count <= cap

    # --- Ride snapshots (the reservation watcher's last view of each ride) ----------------

    async def get_snapshot(self, ride_id: int) -> dict[str, Any] | None:
        raw = await self.redis.get(_k(ride_id, "snap"))
        return orjson.loads(raw) if raw is not None else None

    async def put_snapshot(self, ride_id: int, snapshot: dict[str, Any]) -> None:
        await self.redis.set(_k(ride_id, "snap"), orjson.dumps(snapshot), ex=SNAPSHOT_TTL)

    async def claim_cancellation(self, ride_id: int) -> bool:
        """True for the first caller only. A cancellation can be reported by Laravel's
        explicit ride event and by the reservation watcher; it must reach people once."""
        return bool(await self.redis.set(_k(ride_id, "cancelled"), "1", nx=True, ex=self._ttl_ended))

    async def snapshot_status(self, ride_id: int, status: str) -> None:
        """After limo-rt writes a status itself, so the watcher doesn't report it back."""
        snapshot = await self.get_snapshot(ride_id)
        if snapshot is not None:
            snapshot["status"] = status
            await self.put_snapshot(ride_id, snapshot)

    # --- Trail points waiting for MySQL ---------------------------------------------------

    async def push_trail_point(self, point: dict[str, Any]) -> None:
        await self.redis.rpush(TRAIL_PENDING_KEY, orjson.dumps(point))

    async def pop_trail_points(self, limit: int) -> list[dict[str, Any]]:
        raw = await self.redis.lpop(TRAIL_PENDING_KEY, limit)
        return [orjson.loads(item) for item in raw or []]

    async def requeue_trail_points(self, points: list[dict[str, Any]]) -> None:
        if points:
            await self.redis.lpush(TRAIL_PENDING_KEY, *[orjson.dumps(p) for p in reversed(points)])
