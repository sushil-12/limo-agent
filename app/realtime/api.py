"""REST API of the realtime service (Passport bearer unless noted).

Durable writes come here, never over the socket: status, chat, devices. Each is
idempotent so the apps' offline outboxes can retry freely.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.auth.models import CurrentUser
from app.core.errors import NotFoundError
from app.core.responses import ORJSONResponse
from app.realtime import statuses
from app.realtime.db import utcnow
from app.realtime.deps import AdminDep, ForbiddenError, RtDep, UnprocessableError, UserDep, ViewerDep
from app.realtime.fcm import PushMessage, SendOutcome
from app.realtime.flags import DEFAULT_FLAGS
from app.realtime.rides import RideInfo
from app.realtime.services import Realtime
from app.realtime.share import ShareGrant
from app.realtime.status_writer import StatusContext
from app.realtime.tables import location_points, notifications

router = APIRouter(prefix="/rt/v1", tags=["realtime"])


async def _ride_for(rt: Realtime, ride_id: int, user: CurrentUser) -> RideInfo:
    info = await rt.rides.get(ride_id)
    if info is None:
        raise NotFoundError("Ride not found.")
    if info.role_of(user) is None:
        raise ForbiddenError()
    return info


# --- Status -----------------------------------------------------------------------------


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str = Field(min_length=1, max_length=32)
    timestamp: str | int | float | None = None  # client event time (ISO 8601 or epoch ms)
    stop_info: dict[str, Any] | None = None
    location: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)


@router.post("/driver/rides/{ride_id}/status")
async def update_status(
    ride_id: int,
    body: StatusBody,
    rt: RtDep,
    user: UserDep,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> ORJSONResponse:
    """Port of Laravel's ``POST /api/bookings/status``, for the assigned driver only.

    The response keeps Laravel's field names so the apps' outbox handling is unchanged.
    """
    info = await rt.rides.get(ride_id, fresh=True)
    if info is None:
        raise NotFoundError("Booking not found")
    if info.role_of(user) != "driver":
        raise ForbiddenError()
    if body.status not in statuses.DRIVER_SETTABLE:
        raise UnprocessableError(f"Status {body.status!r} can't be set by the driver app.")

    ctx = StatusContext(
        stop_info=body.stop_info,
        timestamp=body.timestamp,
        driver_user_id=user.id,
        customer_user_id=info.rider_user_ids[0] if info.rider_user_ids else None,
        location=body.location,
        correlation_id=getattr(request.state, "request_id", None),
        idempotency_key=idempotency_key or body.idempotency_key,
        source=str((body.metadata or {}).get("source") or "rt")[:32],
        reason=(body.metadata or {}).get("reason"),
        metadata=body.metadata,
    )
    result = await rt.writer.record(ride_id, body.status, ctx)
    if not result.found:
        raise NotFoundError("Booking not found")
    if not result.accepted:
        raise UnprocessableError(result.message or "Failed to update booking status")
    seq = await rt.events.status_recorded(info, result, location=body.location)
    return ORJSONResponse(
        {
            "success": True,
            "message": result.message or "Booking status updated successfully",
            "data": {
                "bookingId": ride_id,
                "oldStatus": result.previous_status,
                "newStatus": result.status,
                "duplicate": result.duplicate,
                "stale": result.stale,
                "stopInfo": result.stop_info,
                "currentStopIndex": result.current_stop_index,
                "totalStops": result.total_stops,
                "metadata": body.metadata,
                "seq": seq,
            },
        }
    )


# --- Location (REST fallback while the socket is down) -----------------------------------


@router.get("/rides/{ride_id}/location")
async def latest_location(ride_id: int, rt: RtDep, viewer: ViewerDep) -> Response:
    info = await rt.rides.get(ride_id)
    if info is None:
        raise NotFoundError("Ride not found.")
    if isinstance(viewer, ShareGrant):
        if viewer.ride_id != ride_id:
            raise ForbiddenError()
        if info.is_over:
            raise ForbiddenError("This tracking link has expired.", code="share_expired")
    elif info.role_of(viewer) is None:
        raise ForbiddenError()
    latest = await rt.store.get_location(ride_id)
    status = {"status": info.status, "seq": await rt.store.current_seq(ride_id)}
    if latest is None:
        return ORJSONResponse({"location": None, **status})
    return ORJSONResponse({"location": latest["data"], "ts": latest["ts"], **status})


# --- Share links -------------------------------------------------------------------------


@router.post("/rides/{ride_id}/share")
async def create_share_link(ride_id: int, rt: RtDep, user: UserDep) -> ORJSONResponse:
    info = await _ride_for(rt, ride_id, user)
    if info.role_of(user) not in ("rider", "admin"):
        raise ForbiddenError("Only the passenger can share this ride.")
    if info.is_over:
        raise UnprocessableError("This ride has ended.")
    token, expires = rt.shares.mint(ride_id)
    return ORJSONResponse({"token": token, "expires_at": expires})


# --- Chat --------------------------------------------------------------------------------


class ChatBody(BaseModel):
    body: str = Field(min_length=1, max_length=2000)
    client_msg_id: str | None = Field(default=None, max_length=64)


class ReadBody(BaseModel):
    up_to_id: int = Field(gt=0)


def _chat_parties(info: RideInfo, user: CurrentUser) -> tuple[str, list[int]]:
    role = info.role_of(user)
    drivers = [info.driver_user_id] if info.driver_user_id else []
    if role == "driver":
        return "driver", list(info.rider_user_ids)
    if role == "rider":
        return "rider", drivers
    return "admin", [*info.rider_user_ids, *drivers]


@router.get("/rides/{ride_id}/messages")
async def chat_history(
    ride_id: int,
    rt: RtDep,
    user: UserDep,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ORJSONResponse:
    await _ride_for(rt, ride_id, user)
    messages = await rt.chat.history(ride_id, after_id=after_id, limit=limit)
    return ORJSONResponse({"messages": [m.to_wire() for m in messages]})


@router.post("/rides/{ride_id}/messages", status_code=201)
async def post_message(ride_id: int, body: ChatBody, rt: RtDep, user: UserDep) -> ORJSONResponse:
    info = await _ride_for(rt, ride_id, user)
    role, recipients = _chat_parties(info, user)
    message, created = await rt.chat.add(
        ride_id,
        sender_user_id=user.id,
        receiver_user_id=recipients[0] if len(recipients) == 1 else None,
        sender_role=role,
        body=body.body.strip(),
        client_msg_id=body.client_msg_id,
    )
    if created:
        await rt.events.chat_posted(info, message, recipients)
    return ORJSONResponse(
        {"message": message.to_wire(), "created": created}, status_code=201 if created else 200
    )


@router.post("/rides/{ride_id}/messages/read")
async def mark_messages_read(ride_id: int, body: ReadBody, rt: RtDep, user: UserDep) -> ORJSONResponse:
    await _ride_for(rt, ride_id, user)
    count = await rt.chat.mark_read(ride_id, user.id, body.up_to_id)
    return ORJSONResponse({"updated": count})


# --- Devices (FCM tokens) ----------------------------------------------------------------


class DeviceBody(BaseModel):
    token: str = Field(min_length=20, max_length=255)
    app: Literal["driver", "rider"]
    platform: Literal["ios", "android", "web"]
    app_version: str | None = Field(default=None, max_length=32)
    locale: str | None = Field(default=None, max_length=16)


class DeviceRevokeBody(BaseModel):
    token: str = Field(min_length=20, max_length=255)


@router.post("/devices", status_code=204)
async def register_device(body: DeviceBody, rt: RtDep, user: UserDep) -> Response:
    await rt.devices.register(
        user.id,
        token=body.token,
        app=body.app,
        platform=body.platform,
        app_version=body.app_version,
        locale=body.locale,
    )
    return Response(status_code=204)


@router.post("/devices/revoke", status_code=204)
async def revoke_device(body: DeviceRevokeBody, rt: RtDep, user: UserDep) -> Response:
    """Call on logout. (POST, not DELETE with a body: some HTTP stacks drop DELETE bodies.)"""
    await rt.devices.revoke_token(body.token, user_id=user.id)
    return Response(status_code=204)


# --- Notification inbox ------------------------------------------------------------------


def _notification_wire(row: Any) -> dict[str, Any]:
    m = row._mapping
    created = m["created_at"]
    if isinstance(created, str):
        created = datetime.fromisoformat(created)
    read_at = m["read_at"]
    return {
        "id": str(m["id"]),
        "title": m["title"],
        "body": m["body"],
        "kind": m["type"],
        "priority": m["priority"],
        "data": m["data"] or {},
        "ride_id": str(m["reservation_id"]) if m["reservation_id"] else None,
        "read": read_at is not None,
        "at": int(created.replace(tzinfo=UTC).timestamp() * 1000),
    }


@router.get("/notifications")
async def list_notifications(
    rt: RtDep,
    user: UserDep,
    before_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> ORJSONResponse:
    query = select(notifications).where(notifications.c.user_id == user.id)
    if before_id:
        query = query.where(notifications.c.id < before_id)
    query = query.order_by(notifications.c.id.desc()).limit(limit)
    async with rt.engine.connect() as conn:
        rows = (await conn.execute(query)).all()
        unread = len(
            (
                await conn.execute(
                    select(notifications.c.id)
                    .where(notifications.c.user_id == user.id, notifications.c.read_at.is_(None))
                    .limit(100)
                )
            ).all()
        )
    return ORJSONResponse({"notifications": [_notification_wire(r) for r in rows], "unread": unread})


@router.post("/notifications/{notification_id}/read", status_code=204)
async def read_notification(notification_id: int, rt: RtDep, user: UserDep) -> Response:
    async with rt.engine.begin() as conn:
        await conn.execute(
            notifications.update()
            .where(notifications.c.id == notification_id, notifications.c.user_id == user.id)
            .values(read_at=utcnow())
        )
    return Response(status_code=204)


@router.post("/notifications/read-all", status_code=204)
async def read_all_notifications(rt: RtDep, user: UserDep) -> Response:
    async with rt.engine.begin() as conn:
        await conn.execute(
            notifications.update()
            .where(notifications.c.user_id == user.id, notifications.c.read_at.is_(None))
            .values(read_at=utcnow())
        )
    return Response(status_code=204)


# --- Ride trail (admin; replaces the Node ride-history viewer) ----------------------------


@router.get("/rides/{ride_id}/trail")
async def ride_trail(
    ride_id: int,
    rt: RtDep,
    _: AdminDep,
    limit: Annotated[int, Query(ge=1, le=20_000)] = 5000,
) -> ORJSONResponse:
    p = location_points.c
    query = select(p.recorded_at, p.lat, p.lng, p.speed_mps, p.heading, p.accuracy_m, p.source).where(
        p.reservation_id == ride_id
    )
    async with rt.engine.connect() as conn:
        rows = (await conn.execute(query.order_by(p.recorded_at).limit(limit))).all()

    def point(r: Any) -> dict[str, Any]:
        at = r.recorded_at if not isinstance(r.recorded_at, str) else datetime.fromisoformat(r.recorded_at)
        return {
            "at": int(at.replace(tzinfo=UTC).timestamp() * 1000),
            "lat": float(r.lat),
            "lng": float(r.lng),
            "speed_mps": float(r.speed_mps) if r.speed_mps is not None else None,
            "heading": float(r.heading) if r.heading is not None else None,
            "acc_m": float(r.accuracy_m) if r.accuracy_m is not None else None,
            "src": r.source,
        }

    return ORJSONResponse({"ride_id": str(ride_id), "points": [point(r) for r in rows]})


# --- Config, flags and broadcasts ---------------------------------------------------------


@router.get("/config")
async def client_config(rt: RtDep) -> ORJSONResponse:
    """What the apps need to pick a realtime provider. No auth: it holds nothing private,
    and the per-user pilot allowlist is applied by Laravel's mobile/v1/config instead."""
    flags = rt.flags.all()
    return ORJSONResponse(
        {
            "rt": {"provider": flags["rt.provider"], "url": flags["rt.url"]},
            "min_version": flags["app.min_version"],
        }
    )


@router.get("/admin/flags")
async def list_flags(rt: RtDep, _: AdminDep) -> ORJSONResponse:
    return ORJSONResponse({"flags": rt.flags.all(), "known": sorted(DEFAULT_FLAGS)})


class FlagBody(BaseModel):
    value: Any


@router.put("/admin/flags/{name}")
async def set_flag(name: str, body: FlagBody, rt: RtDep, admin: AdminDep) -> ORJSONResponse:
    try:
        await rt.flags.set(name, body.value, user_id=admin.id)
    except ValueError as exc:
        raise UnprocessableError(str(exc)) from exc
    return ORJSONResponse({"name": name, "value": body.value})


BROADCAST_AUDIENCES = {
    "drivers": ("all_drivers",),
    "riders": ("all_riders",),
    "all": ("all_drivers", "all_riders"),
}


class BroadcastBody(BaseModel):
    audience: Literal["drivers", "riders", "all"]
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=500)
    data: dict[str, str] = Field(default_factory=dict)


async def send_broadcast(
    rt: Realtime, audience: str, title: str, body: str, data: dict[str, str]
) -> dict[str, str]:
    message = PushMessage(title=title, body=body, data={**data, "kind": "broadcast"}, high_priority=False)
    results: dict[str, str] = {}
    for topic in BROADCAST_AUDIENCES[audience]:
        outcome = await rt.fcm.send_to_topic(f"{rt.settings.topic_prefix}_{topic}", message)
        results[topic] = outcome.value
    return results


@router.post("/admin/broadcast")
async def broadcast(body: BroadcastBody, rt: RtDep, _: AdminDep) -> ORJSONResponse:
    """Push to every driver and/or rider app (FCM topics ``{env}_all_drivers|all_riders``)."""
    results = await send_broadcast(rt, body.audience, body.title, body.body, body.data)
    ok = all(v == SendOutcome.SENT.value for v in results.values())
    return ORJSONResponse({"sent": ok, "topics": results}, status_code=200 if ok else 502)


@router.get("/health")
async def health(rt: RtDep) -> ORJSONResponse:
    checks: dict[str, str] = {}
    try:
        await rt.redis.ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "down"
    try:
        async with rt.engine.connect() as conn:
            await conn.execute(select(1))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "down"
    healthy = all(v == "ok" for v in checks.values())
    return ORJSONResponse(
        {"status": "ok" if healthy else "degraded", **checks}, status_code=200 if healthy else 503
    )
