"""Connection registry and Redis pub/sub fan-out.

Every message for a ride or a user is PUBLISHed to Redis (``rt:ride:{id}`` /
``rt:user:{uid}``); each process subscribes only to channels that have local listeners
and forwards the raw JSON. One process today, but a second instance works unchanged.

Each connection has a bounded outbound queue drained by its own writer task, so one slow
phone never blocks the others. A full queue closes that connection: the client reconnects
and replays from its last seq, which is cheaper and safer than buffering without limit.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from dataclasses import dataclass, field

import orjson
from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from app.core.logging import get_logger
from app.realtime.store import CONTROL_CHANNEL, ride_channel, user_channel

logger = get_logger(__name__)

OUTBOUND_QUEUE_SIZE = 256


@dataclass(eq=False)
class Outbox:
    """What the hub needs from a connection: an id, a user and a queue of frames."""

    conn_id: str
    user_id: int | None
    # Frame types this connection must never see (share-link viewers: no chat).
    deny_types: frozenset[str] = frozenset()
    queue: asyncio.Queue[str | None] = field(default_factory=lambda: asyncio.Queue(OUTBOUND_QUEUE_SIZE))
    overflowed: bool = False

    def allows(self, frame: str) -> bool:
        if not self.deny_types:
            return True
        try:
            return orjson.loads(frame).get("type") not in self.deny_types
        except orjson.JSONDecodeError:
            return False

    def offer(self, frame: str) -> None:
        if self.overflowed or not self.allows(frame):
            return
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.overflowed = True
            logger.warning("rt_outbox_overflow", conn_id=self.conn_id, user_id=self.user_id)
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(None)  # tells the writer to close

    def close(self) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(None)


class Hub:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._rides: dict[int, set[Outbox]] = defaultdict(set)
        self._users: dict[int, set[Outbox]] = defaultdict(set)
        self._pubsub: PubSub | None = None
        self._reader: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        await self._pubsub.subscribe(CONTROL_CHANNEL)  # keeps the connection subscribed
        self._reader = asyncio.create_task(self._read_loop(), name="rt-hub-reader")

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        for box in {b for boxes in (*self._rides.values(), *self._users.values()) for b in boxes}:
            box.close()
        if self._pubsub is not None:
            await self._pubsub.aclose()

    # --- membership ----------------------------------------------------------------------

    async def add_user(self, box: Outbox) -> None:
        if box.user_id is None:
            return
        await self._join(self._users, box.user_id, box, user_channel(box.user_id))

    async def join_ride(self, ride_id: int, box: Outbox) -> None:
        await self._join(self._rides, ride_id, box, ride_channel(ride_id))

    async def leave_ride(self, ride_id: int, box: Outbox) -> None:
        await self._leave(self._rides, ride_id, box, ride_channel(ride_id))

    async def remove(self, box: Outbox, ride_ids: set[int]) -> None:
        for ride_id in list(ride_ids):
            await self.leave_ride(ride_id, box)
        if box.user_id is not None:
            await self._leave(self._users, box.user_id, box, user_channel(box.user_id))

    def local_ride_listeners(self, ride_id: int) -> int:
        return len(self._rides.get(ride_id, ()))

    def local_user_connected(self, user_id: int) -> bool:
        return bool(self._users.get(user_id))

    async def _join(self, index: dict[int, set[Outbox]], key: int, box: Outbox, channel: str) -> None:
        async with self._lock:
            first = not index.get(key)
            index[key].add(box)
            if first and self._pubsub is not None:
                await self._pubsub.subscribe(channel)

    async def _leave(self, index: dict[int, set[Outbox]], key: int, box: Outbox, channel: str) -> None:
        async with self._lock:
            boxes = index.get(key)
            if not boxes or box not in boxes:
                return
            boxes.discard(box)
            if not boxes:
                del index[key]
                if self._pubsub is not None:
                    await self._pubsub.unsubscribe(channel)

    # --- fan-out -------------------------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._pubsub is not None  # noqa: S101 - set in start()
        while True:
            try:
                message = await self._pubsub.get_message(timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Redis restart: back off and let redis-py reconnect
                logger.warning("rt_pubsub_error", error=str(exc))
                await asyncio.sleep(1.0)
                continue
            if message is None or message.get("type") != "message":
                continue
            channel = message["channel"].decode()
            data = message["data"]
            frame = data.decode() if isinstance(data, bytes) else str(data)
            self._dispatch(channel, frame)

    def _dispatch(self, channel: str, frame: str) -> None:
        kind, _, rest = channel.removeprefix("rt:").partition(":")
        if not rest.isdigit():
            return
        index = self._rides if kind == "ride" else self._users if kind == "user" else None
        if index is None:
            return
        for box in list(index.get(int(rest), ())):
            box.offer(frame)
