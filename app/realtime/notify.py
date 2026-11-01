"""Personal notifications: inbox row, socket frame, push, and the capped SMS fallback.

Decision per user and event:

* always: a ``tbl_rt_notifications`` inbox row and a ``notification`` frame on the
  user's channel (every connected app of that user gets it);
* push to the user's device tokens when no app is connected or all are backgrounded;
  if an app is in the foreground and the event has a ride seq, push only if that seq is
  not acked within RT_PUSH_ACK_TIMEOUT_SECONDS;
* SMS, only for critical events (driver arrived, ride cancelled, driver changed), only
  when the user has no live socket AND no push token, at most RT_SMS_CAP_PER_RIDE per
  ride, and only while the ``rt.sms_fallback`` flag is on.

Never personal pushes to topics: topics are for broadcasts only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.logging import get_logger
from app.realtime.db import utcnow
from app.realtime.devices import DeviceRegistry
from app.realtime.fcm import FcmClient, PushMessage, SendOutcome
from app.realtime.flags import Flags
from app.realtime.laravel import LaravelClient
from app.realtime.protocol import envelope
from app.realtime.store import RtStore
from app.realtime.tables import notifications
from app.realtime.tasks import TaskSet

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Notice:
    title: str
    body: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ride_id: int | None = None
    seq: int | None = None  # ride event seq, for ack-based push suppression
    critical: bool = False
    sms_text: str | None = None
    priority: str = "normal"
    push: bool = True  # False = inbox + socket only
    collapse_key: str | None = None


class Notifier:
    def __init__(
        self,
        engine: AsyncEngine,
        store: RtStore,
        devices: DeviceRegistry,
        fcm: FcmClient,
        laravel: LaravelClient,
        flags: Flags,
        tasks: TaskSet,
        *,
        ack_timeout: float = 5.0,
        sms_cap: int = 3,
    ) -> None:
        self._engine = engine
        self._store = store
        self._devices = devices
        self._fcm = fcm
        self._laravel = laravel
        self._flags = flags
        self._tasks = tasks
        self._ack_timeout = ack_timeout
        self._sms_cap = sms_cap

    async def notify(self, user_id: int, notice: Notice) -> None:
        inbox_id = await self._store_inbox(user_id, notice)
        await self._store.publish_user(
            user_id,
            envelope(
                "notification",
                {
                    "id": str(inbox_id) if inbox_id else None,
                    "title": notice.title,
                    "body": notice.body,
                    "kind": notice.type,
                    "priority": notice.priority,
                    "data": notice.data,
                },
                ride_id=notice.ride_id,
            ),
        )
        presence = await self._store.presence(user_id)
        if presence == "foreground":
            if notice.push and notice.seq is not None and notice.ride_id is not None:
                self._tasks.spawn(self._push_if_unacked(user_id, notice), name=f"rt-ack-wait-{user_id}")
            return
        sent = await self._push(user_id, notice) if notice.push else 0
        if notice.critical and presence is None and sent == 0:
            await self._sms_fallback(user_id, notice)

    async def _push_if_unacked(self, user_id: int, notice: Notice) -> None:
        await asyncio.sleep(self._ack_timeout)
        assert notice.ride_id is not None  # noqa: S101
        assert notice.seq is not None  # noqa: S101
        if await self._store.acked(notice.ride_id, user_id) >= notice.seq:
            return
        await self._push(user_id, notice)

    async def _push(self, user_id: int, notice: Notice) -> int:
        tokens = await self._devices.active_tokens(user_id)
        if not tokens:
            return 0
        data = {k: str(v) for k, v in notice.data.items() if v is not None}
        data.setdefault("kind", notice.type)
        if notice.ride_id is not None:
            data.setdefault("ride_id", str(notice.ride_id))
        _add_legacy_push_keys(data, notice)
        message = PushMessage(
            title=notice.title,
            body=notice.body,
            data=data,
            high_priority=notice.critical or notice.priority == "high",
            collapse_key=notice.collapse_key,
        )
        sent = 0
        for token in tokens:
            outcome = await self._fcm.send_to_token(token, message)
            if outcome is SendOutcome.SENT:
                sent += 1
            elif outcome is SendOutcome.INVALID_TOKEN:
                await self._devices.revoke_token(token)
                logger.info("rt_push_token_revoked", user_id=user_id)
        logger.info("rt_push", user_id=user_id, kind=notice.type, tokens=len(tokens), sent=sent)
        return sent

    async def _sms_fallback(self, user_id: int, notice: Notice) -> None:
        if not notice.sms_text or notice.ride_id is None or not self._flags.enabled("rt.sms_fallback"):
            return
        if await self._devices.active_tokens(user_id):
            return  # has a push token (delivery just failed): don't double up with SMS
        if not await self._store.take_sms_slot(notice.ride_id, self._sms_cap):
            logger.info("rt_sms_cap_reached", ride_id=notice.ride_id, user_id=user_id)
            return
        await self._laravel.send_ride_sms(notice.ride_id, notice.sms_text, user_id)

    async def _store_inbox(self, user_id: int, notice: Notice) -> int | None:
        try:
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    notifications.insert().values(
                        user_id=user_id,
                        title=notice.title[:255],
                        body=notice.body,
                        type=notice.type[:50],
                        priority=notice.priority,
                        data=notice.data or None,
                        reservation_id=notice.ride_id,
                        created_at=utcnow(),
                    )
                )
                return int(result.inserted_primary_key[0])
        except Exception as exc:  # the inbox row must never block delivery
            logger.error("rt_inbox_write_failed", user_id=user_id, error=type(exc).__name__)
            return None


def _add_legacy_push_keys(data: dict[str, str], notice: Notice) -> None:
    """Also name things the way limoRTService's pushes did, so the apps' existing
    background handlers (which read ``bookingId`` and ``type``) understand limo-rt's pushes.

    For a ride status, Node's ``type`` was the status itself (``"ended"``, ``"on_location"``,
    ...), and for chat it was ``"chat_message"``. Keys already in ``data`` (Laravel's own
    legacy notices) are left as they are.
    """
    if notice.ride_id is not None:
        data.setdefault("bookingId", str(notice.ride_id))
    if notice.type == "ride_status" and "status" in data:
        data.setdefault("type", data["status"])
    elif notice.type == "chat":
        data.setdefault("type", "chat_message")
    else:
        data.setdefault("type", notice.type)
