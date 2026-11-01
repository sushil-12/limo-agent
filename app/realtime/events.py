"""What happens after something changes on a ride: stream it, then tell the right people.

Every entry point here runs after the database commit. Broadcasting is awaited (it is a
Redis call, milliseconds); push, SMS and inbox writes run as background tasks so a slow
FCM or Laravel never delays the driver's HTTP response.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.realtime import statuses
from app.realtime.chat import ChatMessage, utc_ms
from app.realtime.laravel import LaravelClient
from app.realtime.location import ACTIVE_RIDES_KEY, TrailSampler
from app.realtime.notify import Notice, Notifier
from app.realtime.protocol import now_ms
from app.realtime.rides import RideDirectory, RideInfo
from app.realtime.status_writer import StatusResult
from app.realtime.store import RtStore
from app.realtime.tasks import TaskSet

logger = get_logger(__name__)

# Rider-facing wording per status: (title, body, critical, push). {driver} = first name.
_STATUS_COPY: dict[str, tuple[str, str, bool, bool]] = {
    statuses.EN_ROUTE_PU: (
        "Your chauffeur is on the way",
        "{driver} is heading to your pickup.",
        False,
        True,
    ),
    statuses.ON_LOCATION: ("Your chauffeur has arrived", "{driver} is waiting at your pickup.", True, True),
    statuses.START_RIDE: ("Your ride has started", "Sit back and enjoy the ride.", False, True),
    statuses.PASSENGER_DROP: ("You've reached a stop", "Stop {stop} of {total}.", False, False),
    statuses.PASSENGER_PICK: ("Back on the road", "Heading to your next stop.", False, False),
    statuses.EN_ROUTE_DO: ("On the way to your destination", "{driver} is taking you there.", False, True),
    statuses.ENDED: ("Ride complete", "Thanks for riding with 1800LIMO.", False, True),
}

_SMS_COPY = {
    statuses.ON_LOCATION: "1800LIMO: Your chauffeur has arrived at your pickup location.",
    "cancelled": "1800LIMO: Your ride #{ride} has been cancelled.",
    "driver_changed": "1800LIMO: Your chauffeur for ride #{ride} has changed.",
}


def _stop_wire(stop: dict[str, Any] | None) -> dict[str, Any] | None:
    if stop is None:
        return None
    return {
        "index": stop["index"],
        "number": stop["stop_number"],
        "total": stop["total_stops"],
        "address": stop["address"],
        "lat": stop["latitude"],
        "lng": stop["longitude"],
    }


class RideEvents:
    def __init__(
        self,
        store: RtStore,
        rides: RideDirectory,
        notifier: Notifier,
        laravel: LaravelClient,
        trail: TrailSampler,
        tasks: TaskSet,
    ) -> None:
        self._store = store
        self._rides = rides
        self._notifier = notifier
        self._laravel = laravel
        self._trail = trail
        self._tasks = tasks

    # --- status --------------------------------------------------------------------------

    async def status_recorded(
        self, info: RideInfo, result: StatusResult, *, location: dict[str, Any] | None
    ) -> int | None:
        """Broadcast an applied status change. Duplicates and stale updates send nothing."""
        if not result.updated or result.status is None:
            return None
        await self._rides.invalidate(info.id)
        status = result.status
        await self._store.snapshot_status(info.id, status)  # not a "Laravel change" for the watcher
        over = status in statuses.TERMINAL_STATUSES
        at = utc_ms(result.event_at) if result.event_at else now_ms()
        message = await self._store.append_event(
            info.id,
            "ride.status",
            {
                "status": status,
                "prev": result.previous_status,
                "at": at,
                "stop": _stop_wire(result.stop_info),
            },
            ride_over=over,
        )
        seq = int(message["seq"])
        if over:
            await self._ride_over(info.id)
        if location is not None:
            lat, lng = (
                location.get("latitude", location.get("lat")),
                location.get("longitude", location.get("lng")),
            )
            if isinstance(lat, int | float) and isinstance(lng, int | float) and lat and lng:
                await self._trail.status_point(info.id, info.driver_user_id, float(lat), float(lng))

        copy = _STATUS_COPY.get(status)
        if copy is not None:
            title, body, critical, push = copy
            stop = result.stop_info or {}
            text = body.format(
                driver=info.driver_first_name or "Your chauffeur",
                stop=stop.get("stop_number", ""),
                total=stop.get("total_stops", ""),
            )
            for rider in info.rider_user_ids:
                notice = Notice(
                    title=title,
                    body=text,
                    type="ride_status",
                    data={"status": status, "seq": seq},
                    ride_id=info.id,
                    seq=seq,
                    critical=critical,
                    sms_text=_SMS_COPY.get(status),
                    priority="high" if critical else "normal",
                    push=push,
                    collapse_key=f"ride-{info.id}-status",
                )
                self._tasks.spawn(self._notifier.notify(rider, notice), name=f"rt-notify-{info.id}")
        if result.completed_transition:
            self._tasks.spawn(self._laravel.ride_completed_sms(info.id), name=f"rt-completed-sms-{info.id}")
        return seq

    # --- changes Laravel makes (admin, dispatch, rider cancel) ---------------------------

    async def ride_cancelled(self, ride_id: int, *, by: str) -> None:
        info = await self._rides.get(ride_id, fresh=True)
        if info is None:
            return
        message = await self._store.append_event(
            ride_id, "ride.cancelled", {"by": by, "at": now_ms()}, ride_over=True
        )
        await self._ride_over(ride_id)
        recipients = [*info.rider_user_ids, *([info.driver_user_id] if info.driver_user_id else [])]
        for user_id in recipients:
            notice = Notice(
                title="Ride cancelled",
                body=f"Ride #{ride_id} has been cancelled.",
                type="ride_cancelled",
                data={"seq": message["seq"]},
                ride_id=ride_id,
                seq=int(message["seq"]),
                critical=True,
                sms_text=_SMS_COPY["cancelled"].format(ride=ride_id)
                if user_id in info.rider_user_ids
                else None,
                priority="high",
            )
            self._tasks.spawn(self._notifier.notify(user_id, notice), name=f"rt-cancel-{ride_id}")

    async def driver_changed(self, ride_id: int, *, previous_driver_user_id: int | None) -> None:
        info = await self._rides.get(ride_id, fresh=True)
        if info is None:
            return
        await self._store.redis.delete(f"rt:ride:{ride_id}:loc")  # the old driver's position
        self._trail.forget(ride_id)
        message = await self._store.append_event(
            ride_id,
            "ride.driver_changed",
            {
                "driver_id": str(info.driver_user_id) if info.driver_user_id else None,
                "name": info.driver_first_name,
            },
        )
        seq = int(message["seq"])
        for rider in info.rider_user_ids:
            notice = Notice(
                title="Your chauffeur has changed",
                body=f"{info.driver_first_name or 'A new chauffeur'} will now take your ride.",
                type="driver_changed",
                data={"seq": seq},
                ride_id=ride_id,
                seq=seq,
                critical=True,
                sms_text=_SMS_COPY["driver_changed"].format(ride=ride_id),
                priority="high",
            )
            self._tasks.spawn(self._notifier.notify(rider, notice), name=f"rt-driver-changed-{ride_id}")
        if previous_driver_user_id and previous_driver_user_id != info.driver_user_id:
            notice = Notice(
                title="Ride reassigned",
                body=f"Ride #{ride_id} is no longer assigned to you.",
                type="ride_unassigned",
                ride_id=ride_id,
                critical=True,
                priority="high",
            )
            self._tasks.spawn(
                self._notifier.notify(previous_driver_user_id, notice), name=f"rt-unassigned-{ride_id}"
            )

    async def ride_updated(self, ride_id: int, *, status: str | None) -> None:
        """Laravel changed the booking (admin status edit, booking edit): resync clients."""
        await self._rides.invalidate(ride_id)
        info = await self._rides.get(ride_id, fresh=True)
        if info is None:
            return
        data: dict[str, Any] = {"status": status or info.status, "prev": None, "at": now_ms(), "stop": None}
        await self._store.append_event(ride_id, "ride.status", data, ride_over=info.is_over)
        if info.is_over:
            await self._ride_over(ride_id)

    # --- chat and notices ---------------------------------------------------------------

    async def chat_posted(self, info: RideInfo, message: ChatMessage, recipients: list[int]) -> int:
        event = await self._store.append_event(info.id, "chat.message", message.to_wire())
        seq = int(event["seq"])
        sender = "Your chauffeur" if message.sender_role == "driver" else "Your passenger"
        if message.sender_role == "admin":
            sender = "1800LIMO"
        for user_id in recipients:
            notice = Notice(
                title=f"New message from {sender}",
                body=message.body[:180],
                type="chat",
                data={"message_id": message.id, "seq": seq},
                ride_id=info.id,
                seq=seq,
                collapse_key=f"ride-{info.id}-chat",
            )
            self._tasks.spawn(self._notifier.notify(user_id, notice), name=f"rt-chat-{info.id}")
        return seq

    async def approaching(self, info: RideInfo, *, leg: str, threshold: str, meters: int) -> None:
        message = await self._store.append_event(
            info.id,
            "ride.notice",
            {"kind": "approaching", "threshold": threshold, "meters": meters, "leg": leg},
        )
        if leg != "pickup":
            return  # the passenger is in the car: the map notice is enough
        seq = int(message["seq"])
        name = info.driver_first_name or "Your chauffeur"
        title, body = (
            ("Your chauffeur is about a mile away", f"{name} will be there in a few minutes.")
            if threshold == "1mi"
            else ("Your chauffeur is almost there", f"{name} is less than a quarter mile away.")
        )
        for rider in info.rider_user_ids:
            notice = Notice(
                title=title,
                body=body,
                type="driver_approaching",
                data={"threshold": threshold, "seq": seq},
                ride_id=info.id,
                seq=seq,
                priority="high",
                collapse_key=f"ride-{info.id}-approach",
            )
            self._tasks.spawn(self._notifier.notify(rider, notice), name=f"rt-approach-{info.id}")

    async def _ride_over(self, ride_id: int) -> None:
        await self._store.mark_ride_over(ride_id)
        await self._store.redis.zrem(ACTIVE_RIDES_KEY, str(ride_id))
        self._trail.forget(ride_id)
