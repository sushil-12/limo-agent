"""Laravel -> limo-rt. Every route needs ``x-secret: RT_INTERNAL_SECRET``, and the proxy
must only expose these paths on the private network (deploy/nginx/limo-rt.conf).

* ``/internal/v1/*``: new endpoints for ride changes Laravel makes itself.
* ``/api/notifications/*``, ``/api/topics/*``, ``/api/health``: drop-in replacements for
  the limoRTService endpoints Laravel calls today, so retiring Node only needs Laravel's
  NOTIFICATION_SERVICE_URL / NODEJS_API_URL repointed (plus the new secret).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.responses import ORJSONResponse
from app.realtime.api import send_broadcast
from app.realtime.deps import InternalDep, RtDep, UnprocessableError
from app.realtime.fcm import PushMessage, SendOutcome
from app.realtime.notify import Notice
from app.realtime.tables import notifications

router = APIRouter(dependencies=[InternalDep], include_in_schema=False)
public_router = APIRouter(include_in_schema=False)

_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --- New internal endpoints ------------------------------------------------------------


class RideEventBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ride_id: int = Field(gt=0)
    kind: Literal["cancelled", "driver_changed", "updated"]
    by: Literal["admin", "rider", "driver", "system"] = "admin"
    previous_driver_user_id: int | None = None
    status: str | None = Field(default=None, max_length=32)


@router.post("/internal/v1/ride-events", status_code=202)
async def ride_event(body: RideEventBody, rt: RtDep) -> ORJSONResponse:
    """Call after Laravel commits a cancellation, a reassignment or an admin status edit."""
    await rt.rides.invalidate(body.ride_id)
    if body.kind == "cancelled":
        await rt.events.ride_cancelled(body.ride_id, by=body.by)
    elif body.kind == "driver_changed":
        await rt.events.driver_changed(body.ride_id, previous_driver_user_id=body.previous_driver_user_id)
    else:
        await rt.events.ride_updated(body.ride_id, status=body.status)
    return ORJSONResponse({"accepted": True}, status_code=202)


@router.post("/internal/v1/rides/{ride_id}/share")
async def internal_share(ride_id: int, rt: RtDep) -> ORJSONResponse:
    """Mint a tracking link for an SMS or email Laravel sends."""
    info = await rt.rides.get(ride_id)
    if info is None or info.is_over:
        raise UnprocessableError("Ride not found or already over.")
    token, expires = rt.shares.mint(ride_id)
    return ORJSONResponse({"token": token, "expires_at": expires})


# --- limoRTService-compatible endpoints ------------------------------------------------


class LegacySend(BaseModel):
    model_config = ConfigDict(extra="ignore")

    userId: int | str  # noqa: N815 - legacy field name
    title: str = Field(min_length=1, max_length=255)
    message: str = Field(min_length=1, max_length=4000)
    type: str = "info"
    priority: str = "normal"
    data: dict[str, Any] = Field(default_factory=dict)
    channel: list[str] | str | None = None


@router.post("/api/notifications/send", status_code=202)
async def legacy_send(body: LegacySend, rt: RtDep) -> ORJSONResponse:
    """Same contract as Node: acknowledge first, deliver in the background."""
    try:
        user_id = int(body.userId)
    except ValueError as exc:
        raise UnprocessableError("userId must be numeric") from exc
    ride_id = body.data.get("bookingId") or body.data.get("booking_id") or body.data.get("reservation_id")
    notice = Notice(
        title=body.title,
        body=body.message,
        type=body.type[:50],
        data={k: v for k, v in body.data.items() if isinstance(v, str | int | float | bool)},
        ride_id=int(ride_id) if str(ride_id or "").isdigit() else None,
        priority="high" if body.priority in ("high", "urgent") else "normal",
    )
    rt.tasks.spawn(rt.notifier.notify(user_id, notice), name=f"rt-legacy-send-{user_id}")
    accepted = _now_iso()
    return ORJSONResponse(
        {
            "success": True,
            "message": "Notification accepted for delivery",
            "data": {
                "userId": body.userId,
                "title": body.title,
                "message": body.message,
                "type": body.type,
                "priority": body.priority,
                "data": body.data,
                "acceptedAt": accepted,
            },
            "timestamp": accepted,
        },
        status_code=202,
    )


class LegacyTopic(BaseModel):
    model_config = ConfigDict(extra="ignore")

    topic: str
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=4000)
    data: dict[str, Any] = Field(default_factory=dict)
    priority: str = "normal"


@router.post("/api/notifications/fcm-topic")
async def legacy_fcm_topic(body: LegacyTopic, rt: RtDep) -> ORJSONResponse:
    """Broadcast topics only. Per-user topics ({env}_{userId}) are refused: personal
    notifications go to device tokens via /api/notifications/send."""
    if not _TOPIC.match(body.topic):
        raise UnprocessableError("Invalid topic format.")
    if body.topic.isdigit():
        raise UnprocessableError("Per-user topics are no longer supported; send to the user instead.")
    message = PushMessage(
        title=body.title,
        body=body.body,
        data={k: str(v) for k, v in body.data.items()},
        high_priority=body.priority == "high",
    )
    outcome = await rt.fcm.send_to_topic(f"{rt.settings.topic_prefix}_{body.topic}", message)
    ok = outcome is SendOutcome.SENT
    return ORJSONResponse(
        {
            "success": ok,
            "message": "FCM topic notification sent successfully"
            if ok
            else f"FCM topic send {outcome.value}",
            "data": {"topic": body.topic, "sentAt": _now_iso()},
            "timestamp": _now_iso(),
        },
        status_code=200 if ok else 502,
    )


class LegacyUserType(BaseModel):
    model_config = ConfigDict(extra="ignore")

    userTypes: list[str]  # noqa: N815
    title: str = Field(min_length=1, max_length=255)
    message: str = Field(min_length=1, max_length=4000)
    data: dict[str, Any] = Field(default_factory=dict)


@router.post("/api/notifications/user-type")
async def legacy_user_type(body: LegacyUserType, rt: RtDep) -> ORJSONResponse:
    """Was a TODO in Node. Now maps driver/customer to the broadcast topics."""
    kinds = {t.lower() for t in body.userTypes}
    wants_drivers = bool(kinds & {"driver", "drivers"})
    wants_riders = bool(kinds & {"customer", "customers", "rider", "riders", "user", "users"})
    if not (wants_drivers or wants_riders):
        raise UnprocessableError("userTypes must include driver and/or customer.")
    audience = "all" if wants_drivers and wants_riders else "drivers" if wants_drivers else "riders"
    data = {k: str(v) for k, v in body.data.items()}
    results = await send_broadcast(rt, audience, body.title, body.message, data)
    ok = all(v == SendOutcome.SENT.value for v in results.values())
    return ORJSONResponse(
        {"success": ok, "data": results, "timestamp": _now_iso()}, status_code=200 if ok else 502
    )


@router.post("/api/notifications/topics")
async def legacy_topics(rt: RtDep) -> ORJSONResponse:
    """Node's Mongo-topic fan-out had no subscribed devices; accepted and ignored."""
    return ORJSONResponse({"success": True, "data": {"delivered": 0}, "timestamp": _now_iso()})


@router.get("/api/notifications/history")
async def legacy_history(
    rt: RtDep,
    userId: Annotated[int, Query(gt=0)],  # noqa: N803
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ORJSONResponse:
    query = (
        select(notifications)
        .where(notifications.c.user_id == userId)
        .order_by(notifications.c.id.desc())
        .limit(limit)
    )
    async with rt.engine.connect() as conn:
        rows = (await conn.execute(query)).all()
    items = [
        {
            "id": str(r.id),
            "title": r.title,
            "message": r.body,
            "type": r.type,
            "priority": r.priority,
            "data": r.data or {},
            "read": r.read_at is not None,
            "createdAt": str(r.created_at),
        }
        for r in rows
    ]
    return ORJSONResponse({"success": True, "data": items, "timestamp": _now_iso()})


@router.post("/api/topics/subscribe")
@router.post("/api/topics/unsubscribe")
async def legacy_topic_membership(rt: RtDep) -> ORJSONResponse:
    """Server-side topic membership is gone: apps subscribe to broadcast topics themselves."""
    return ORJSONResponse({"success": True, "data": {"topics": []}, "timestamp": _now_iso()})


@router.get("/api/topics/user/{user_id}")
async def legacy_user_topics(user_id: int, rt: RtDep) -> ORJSONResponse:
    return ORJSONResponse({"success": True, "data": {"userId": str(user_id), "topics": []}})


@router.get("/api/topics/user/{user_id}/check/{topic}")
async def legacy_user_topic_check(user_id: int, topic: str, rt: RtDep) -> ORJSONResponse:
    return ORJSONResponse({"success": True, "data": {"subscribed": False}})


@public_router.get("/api/health")
async def legacy_health() -> ORJSONResponse:
    """Laravel's health probe sends no secret."""
    return ORJSONResponse({"success": True, "status": "healthy", "timestamp": _now_iso()})
