"""The WebSocket endpoint: ``GET /rt/v1/ws``.

Session lifecycle:

1. connect, then send ``hello`` with a Passport bearer token or a share token within
   RT_HELLO_TIMEOUT_SECONDS; the server answers ``welcome`` (server clock, active ride);
2. ``subscribe {ride_id, last_seq}``: access is checked, missed events are replayed in
   order, the latest location follows as a snapshot, then ``subscribed``;
3. live frames. Clients dedupe durable events by seq (a frame can arrive both in the
   replay and live), and treat 30 s without any frame as a dead connection;
4. any client frame refreshes presence. No frame for RT_IDLE_TIMEOUT_SECONDS closes the
   socket. Clients send ``ping`` every ~20 s when they have nothing else to send.

Protocol-level WebSocket pings are handled by uvicorn (``--ws-ping-interval``).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.auth.models import CurrentUser
from app.core.errors import ServiceUnavailableError
from app.core.logging import get_logger
from app.realtime.hub import Outbox
from app.realtime.location import snapshot_frame
from app.realtime.protocol import (
    AppStateData,
    HelloData,
    Inbound,
    LocationData,
    ProtocolError,
    SubscribeData,
    dumps,
    envelope,
    error_frame,
    new_id,
    now_ms,
    parse_data,
    parse_frame,
)
from app.realtime.rides import RideInfo
from app.realtime.services import Realtime
from app.realtime.share import ShareGrant, is_share_token

logger = get_logger(__name__)
router = APIRouter()

CLOSE_UNAUTHORIZED = 4401
CLOSE_BAD_HELLO = 4400
CLOSE_SHARE_EXPIRED = 4410
CLOSE_TRY_AGAIN = 1013
PRESENCE_REFRESH_SECONDS = 10.0
VIEWER_DENY = frozenset({"chat.message", "notification"})


@dataclass(eq=False)
class Session:
    rt: Realtime
    ws: WebSocket
    box: Outbox
    app: str = "unknown"
    user: CurrentUser | None = None
    share: ShareGrant | None = None
    rides: set[int] = field(default_factory=set)
    app_state: str = "foreground"
    _presence_at: float = 0.0
    _loc_window: float = 0.0
    _loc_count: int = 0

    @property
    def user_id(self) -> int | None:
        return self.user.id if self.user else None

    def send(self, frame: str) -> None:
        self.box.offer(frame)

    def error(self, code: str, *, retry: bool = False, ride_id: int | None = None) -> None:
        self.send(error_frame(code, retry=retry, ride_id=ride_id))  # type: ignore[arg-type]


@router.websocket("/rt/v1/ws")
async def realtime_socket(websocket: WebSocket) -> None:
    rt: Realtime = websocket.app.state.rt
    await websocket.accept()
    session = Session(rt=rt, ws=websocket, box=Outbox(conn_id=new_id(), user_id=None))
    try:
        if not await _handshake(session):
            return
        writer = asyncio.create_task(_writer(session), name=f"rt-ws-writer-{session.box.conn_id}")
        try:
            await _reader(session)
        finally:
            session.box.close()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(writer, timeout=2.0)
            writer.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        await _cleanup(session)


async def _handshake(session: Session) -> bool:
    rt, ws = session.rt, session.ws
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=rt.settings.rt_hello_timeout_seconds)
        frame = parse_frame(raw)
        if frame.type != "hello":
            raise ProtocolError("hello_required")
        hello = parse_data(HelloData, frame.data)
    except (TimeoutError, ProtocolError):
        await ws.send_text(error_frame("hello_required"))
        await ws.close(CLOSE_BAD_HELLO)
        return False

    session.app = hello.app
    if is_share_token(hello.token):
        grant = rt.shares.verify(hello.token)
        info = await rt.rides.get(grant.ride_id) if grant else None
        if grant is None or info is None:
            await ws.send_text(error_frame("unauthorized"))
            await ws.close(CLOSE_UNAUTHORIZED)
            return False
        if info.is_over:
            await ws.send_text(error_frame("share_expired"))
            await ws.close(CLOSE_SHARE_EXPIRED)
            return False
        session.share = grant
        session.box.deny_types = VIEWER_DENY
        welcome = {"user_id": None, "role": "viewer", "ride_id": str(grant.ride_id)}
    else:
        try:
            user = await rt.verifier.verify(hello.token)
        except ServiceUnavailableError:
            await ws.send_text(error_frame("server_error", retry=True))
            await ws.close(CLOSE_TRY_AGAIN)
            return False
        if user is None:
            await ws.send_text(error_frame("unauthorized"))
            await ws.close(CLOSE_UNAUTHORIZED)
            return False
        session.user = user
        session.box.user_id = user.id
        active = await rt.rides.active_ride_for(user)
        welcome = {
            "user_id": str(user.id),
            "role": user.role,
            "active_ride": {"ride_id": str(active[0]), "status": active[1]} if active else None,
        }
        await rt.hub.add_user(session.box)
        await _touch_presence(session, force=True)

    welcome |= {
        "server_time": now_ms(),
        "ping_interval_s": int(rt.settings.rt_ping_interval_seconds),
        "idle_timeout_s": int(rt.settings.rt_idle_timeout_seconds),
    }
    await ws.send_text(dumps(envelope("welcome", welcome)))
    logger.info("rt_ws_connected", conn_id=session.box.conn_id, user_id=session.user_id, app=session.app)
    return True


async def _writer(session: Session) -> None:
    queue = session.box.queue
    while True:
        frame = await queue.get()
        if frame is None:
            with contextlib.suppress(Exception):
                await session.ws.close(CLOSE_TRY_AGAIN if session.box.overflowed else 1000)
            return
        await session.ws.send_text(frame)


async def _reader(session: Session) -> None:
    idle = session.rt.settings.rt_idle_timeout_seconds
    while True:
        try:
            raw = await asyncio.wait_for(session.ws.receive_text(), timeout=idle)
        except TimeoutError:
            logger.info("rt_ws_idle_timeout", conn_id=session.box.conn_id, user_id=session.user_id)
            return
        except WebSocketDisconnect:
            return
        if session.box.overflowed:
            return
        try:
            frame = parse_frame(raw)
            await _touch_presence(session)
            await _handle(session, frame)
        except ProtocolError as exc:
            session.error(exc.code, retry=exc.retry)
        except ServiceUnavailableError:
            session.error("server_error", retry=True)


async def _handle(session: Session, frame: Inbound) -> None:
    rt = session.rt
    match frame.type:
        case "ping":
            session.send(dumps(envelope("pong", {"server_time": now_ms()})))
        case "subscribe":
            await _subscribe(session, _require_ride(frame), parse_data(SubscribeData, frame.data))
        case "unsubscribe":
            ride_id = _require_ride(frame)
            session.rides.discard(ride_id)
            await rt.hub.leave_ride(ride_id, session.box)
        case "ack":
            ride_id = _require_ride(frame)
            if session.user is not None and frame.seq is not None and ride_id in session.rides:
                await rt.store.record_ack(ride_id, session.user.id, frame.seq)
        case "app_state":
            session.app_state = parse_data(AppStateData, frame.data).state
            await _touch_presence(session, force=True)
        case "loc.update":
            await _location(session, _require_ride(frame), frame)
        case _:
            raise ProtocolError("bad_payload")


def _require_ride(frame: Inbound) -> int:
    if frame.ride_id is None or frame.ride_id <= 0:
        raise ProtocolError("bad_payload")
    return frame.ride_id


async def _subscribe(session: Session, ride_id: int, request: SubscribeData) -> None:
    rt = session.rt
    info = await rt.rides.get(ride_id)
    if not _may_view(session, info):
        if session.share is not None and info is not None and info.is_over:
            session.error("share_expired", ride_id=ride_id)
            session.box.close()
            return
        session.error("forbidden_ride", ride_id=ride_id)
        return
    assert info is not None  # noqa: S101 - _may_view
    await rt.hub.join_ride(ride_id, session.box)  # join first: nothing published from here on is lost
    session.rides.add(ride_id)

    frames, gap = await rt.store.replay(ride_id, request.last_seq)
    if gap:
        session.send(dumps(envelope("resync.required", {"reason": "gap_too_large"}, ride_id=ride_id)))
    for frame in frames:
        session.send(frame)
    latest = await rt.store.get_location(ride_id)
    if latest is not None and not info.is_over:
        session.send(dumps(snapshot_frame(latest)))
    session.send(
        dumps(
            envelope(
                "subscribed",
                {"seq": await rt.store.current_seq(ride_id), "status": info.status},
                ride_id=ride_id,
            )
        )
    )
    if session.user is not None and info.role_of(session.user) == "driver":
        await rt.redis.publish(
            f"rt:ride:{ride_id}", dumps(envelope("driver.presence", {"online": True}, ride_id=ride_id))
        )


def _may_view(session: Session, info: RideInfo | None) -> bool:
    if info is None:
        return False
    if session.share is not None:
        return session.share.ride_id == info.id and not info.is_over
    return session.user is not None and info.role_of(session.user) is not None


async def _location(session: Session, ride_id: int, frame: Inbound) -> None:
    if session.user is None or session.user.role != "driver":
        raise ProtocolError("forbidden_ride")
    now = time.monotonic()
    if now - session._loc_window >= 1.0:
        session._loc_window, session._loc_count = now, 0
    session._loc_count += 1
    if session._loc_count > session.rt.settings.rt_location_max_per_second:
        return  # silently drop the excess; the next second's fix supersedes it anyway
    fix = parse_data(LocationData, frame.data)
    await session.rt.location.ingest(session.user, ride_id, fix)


async def _touch_presence(session: Session, *, force: bool = False) -> None:
    if session.user is None:
        return
    now = time.monotonic()
    if not force and now - session._presence_at < PRESENCE_REFRESH_SECONDS:
        return
    session._presence_at = now
    await session.rt.store.touch_presence(session.user.id, session.box.conn_id, session.app_state)


async def _cleanup(session: Session) -> None:
    rt = session.rt
    with contextlib.suppress(Exception):
        await rt.hub.remove(session.box, session.rides)
    with contextlib.suppress(Exception):
        if session.user is not None:
            await rt.store.drop_presence(session.user.id, session.box.conn_id)
            if session.user.role == "driver" and not rt.hub.local_user_connected(session.user.id):
                for ride_id in session.rides:
                    await rt.redis.publish(
                        f"rt:ride:{ride_id}",
                        dumps(
                            envelope(
                                "driver.presence", {"online": False, "last_seen": now_ms()}, ride_id=ride_id
                            )
                        ),
                    )
    logger.info("rt_ws_closed", conn_id=session.box.conn_id, user_id=session.user_id)
