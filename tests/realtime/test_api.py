"""REST API: status writes, chat, devices, inbox, push/SMS decisions, internal API, jobs."""

from __future__ import annotations

from typing import Any

import pytest
import respx
from httpx import Response

from app.realtime.fcm import PushMessage, SendOutcome
from tests.conftest import TokenFactory
from tests.realtime.conftest import (
    ADMIN,
    CHARTER_RIDE,
    DRIVER,
    INTERNAL_SECRET,
    LIVE_RIDE,
    OTHER_DRIVER,
    RIDER,
    STRANGER,
    RtHarness,
    bearer,
    hello,
    loc,
    receive_until,
)

LARAVEL = "http://laravel.test/api/"


@pytest.fixture
def pushes(h: RtHarness) -> list[tuple[str, PushMessage]]:
    """Stub FCM: record every send instead of calling Google."""
    sent: list[tuple[str, PushMessage]] = []

    async def to_token(token: str, message: PushMessage) -> SendOutcome:
        sent.append((token, message))
        return SendOutcome.INVALID_TOKEN if token.startswith("dead") else SendOutcome.SENT

    async def to_topic(topic: str, message: PushMessage) -> SendOutcome:
        sent.append((f"/topics/{topic}", message))
        return SendOutcome.SENT

    h.rt.fcm.send_to_token = to_token  # type: ignore[method-assign]
    h.rt.fcm.send_to_topic = to_topic  # type: ignore[method-assign]
    return sent


@pytest.fixture
def laravel() -> Any:
    with respx.mock(base_url=LARAVEL, assert_all_called=False) as router:
        router.post("sms/send").mock(return_value=Response(200, json={"success": True}))
        router.post(f"internal/rides/{LIVE_RIDE}/completed-sms").mock(return_value=Response(200, json={}))
        yield router


def status(h: RtHarness, token: str, value: str, ride: int = LIVE_RIDE, **extra: Any) -> Any:
    return h.client.post(
        f"/rt/v1/driver/rides/{ride}/status", json={"status": value, **extra}, headers=bearer(token)
    )


def register(h: RtHarness, token: str, fcm: str, app: str = "rider") -> None:
    r = h.client.post(
        "/rt/v1/devices", json={"token": fcm, "app": app, "platform": "ios"}, headers=bearer(token)
    )
    assert r.status_code == 204, r.text


# --- status endpoint ----------------------------------------------------------------------


def test_status_response_keeps_laravel_shape(
    h: RtHarness, rt_token: TokenFactory, pushes: list, laravel: Any
) -> None:
    r = status(h, rt_token(DRIVER), "on_location", timestamp="2026-10-31T10:05:00Z")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["bookingId"] == LIVE_RIDE
    assert data["oldStatus"] == "en_route_pu"
    assert data["newStatus"] == "on_location"
    assert data["duplicate"] is False
    assert data["stale"] is False
    assert data["seq"] == 1


def test_status_retry_with_same_idempotency_key(h: RtHarness, rt_token: TokenFactory, pushes: list) -> None:
    token = rt_token(DRIVER)
    headers = {**bearer(token), "Idempotency-Key": "abc-1"}
    first = h.client.post(
        f"/rt/v1/driver/rides/{LIVE_RIDE}/status", json={"status": "on_location"}, headers=headers
    )
    again = h.client.post(
        f"/rt/v1/driver/rides/{LIVE_RIDE}/status", json={"status": "on_location"}, headers=headers
    )
    assert first.json()["data"]["duplicate"] is False
    assert again.status_code == 200
    assert again.json()["data"]["duplicate"] is True
    assert again.json()["data"]["seq"] is None  # nothing broadcast twice


@pytest.mark.parametrize("user", [OTHER_DRIVER, RIDER, STRANGER])
def test_only_the_assigned_driver_can_change_status(h: RtHarness, rt_token: TokenFactory, user: int) -> None:
    assert status(h, rt_token(user), "on_location").status_code == 403
    assert h.sql("SELECT booking_status FROM tbl_reservations WHERE id=?", (LIVE_RIDE,)) == [("en_route_pu",)]


@pytest.mark.parametrize("value", ["finalized", "cancelled", "pending", "teleported"])
def test_driver_cannot_set_dispatch_statuses(h: RtHarness, rt_token: TokenFactory, value: str) -> None:
    assert status(h, rt_token(DRIVER), value).status_code == 422


def test_unauthenticated_status_is_401(h: RtHarness) -> None:
    assert (
        h.client.post(f"/rt/v1/driver/rides/{LIVE_RIDE}/status", json={"status": "ended"}).status_code == 401
    )


def test_status_change_reaches_subscribed_rider(h: RtHarness, rt_token: TokenFactory, pushes: list) -> None:
    with h.client.websocket_connect("/rt/v1/ws") as ws:
        ws.send_json(hello(rt_token(RIDER)))
        receive_until(ws, "welcome")
        ws.send_json({"v": 1, "type": "subscribe", "ride_id": str(LIVE_RIDE)})
        receive_until(ws, "subscribed")
        status(h, rt_token(DRIVER), "on_location")
        frame = receive_until(ws, "ride.status")
        note = receive_until(ws, "notification")
        ws.send_json({"v": 1, "type": "ack", "ride_id": str(LIVE_RIDE), "seq": frame["seq"]})
        ws.send_json({"v": 1, "type": "ping"})
        receive_until(ws, "pong")
        h.drain()
    assert frame["data"]["status"] == "on_location"
    assert frame["seq"] == 1
    assert note["data"]["title"] == "Your chauffeur has arrived"
    assert pushes == []  # foreground and acked in time: no push


def test_backgrounded_rider_gets_push_and_inbox_row(
    h: RtHarness, rt_token: TokenFactory, pushes: list
) -> None:
    rider = rt_token(RIDER)
    register(h, rider, "rider-token-0000000000000001")
    status(h, rt_token(DRIVER), "on_location")
    h.drain()
    assert [t for t, _ in pushes] == ["rider-token-0000000000000001"]
    assert pushes[0][1].title == "Your chauffeur has arrived"
    assert pushes[0][1].data["ride_id"] == str(LIVE_RIDE)
    # limoRTService's key names too, for the apps' existing background push handlers.
    assert pushes[0][1].data["bookingId"] == str(LIVE_RIDE)
    assert pushes[0][1].data["type"] == "on_location"
    inbox = h.client.get("/rt/v1/notifications", headers=bearer(rider)).json()
    assert inbox["unread"] == 1
    assert inbox["notifications"][0]["kind"] == "ride_status"


def test_foreground_rider_who_does_not_ack_gets_push(
    h: RtHarness, rt_token: TokenFactory, pushes: list
) -> None:
    rider = rt_token(RIDER)
    register(h, rider, "rider-token-0000000000000002")
    with h.client.websocket_connect("/rt/v1/ws") as ws:
        ws.send_json(hello(rider))
        receive_until(ws, "welcome")
        status(h, rt_token(DRIVER), "on_location")
        h.drain()  # waits out RT_PUSH_ACK_TIMEOUT_SECONDS (0.2 s in tests)
    assert [t for t, _ in pushes] == ["rider-token-0000000000000002"]


def test_dead_token_is_revoked(h: RtHarness, rt_token: TokenFactory, pushes: list) -> None:
    register(h, rt_token(RIDER), "dead-token-000000000000000001")
    status(h, rt_token(DRIVER), "on_location")
    h.drain()
    assert h.sql("SELECT revoked_at IS NOT NULL FROM tbl_rt_user_devices") == [(1,)]


def test_sms_fallback_only_without_socket_or_token_and_capped(
    h: RtHarness, rt_token: TokenFactory, pushes: list, laravel: Any
) -> None:
    driver = rt_token(DRIVER)
    for _ in range(5):  # arrived, re-run pickup leg, arrived again ...
        status(h, driver, "on_location")
        status(h, driver, "en_route_pu")
        h.drain()
    calls = laravel.routes[0].calls
    # Two riders (booker + travel-agent passenger), arrival SMS capped at 3 per ride.
    assert 1 <= len(calls) <= 3
    body = calls[0].request.content
    assert b'"message_type":"ride_status"' in body
    assert b"arrived" in body


def test_sms_fallback_respects_kill_switch(
    h: RtHarness, rt_token: TokenFactory, pushes: list, laravel: Any
) -> None:
    admin = rt_token(ADMIN)
    assert (
        h.client.put(
            "/rt/v1/admin/flags/rt.sms_fallback", json={"value": False}, headers=bearer(admin)
        ).status_code
        == 200
    )
    status(h, rt_token(DRIVER), "on_location")
    h.drain()
    assert laravel.routes[0].calls == []


def test_ride_end_triggers_completed_sms_once(
    h: RtHarness, rt_token: TokenFactory, pushes: list, laravel: Any
) -> None:
    driver = rt_token(DRIVER)
    status(h, driver, "ended")
    status(h, driver, "ended")
    h.drain()
    assert len(laravel.routes[1].calls) == 1
    assert laravel.routes[1].calls[0].request.headers["x-secret"] == INTERNAL_SECRET


def test_status_location_is_kept_as_trail_point(h: RtHarness, rt_token: TokenFactory, pushes: list) -> None:
    status(h, rt_token(DRIVER), "on_location", location={"latitude": 40.75, "longitude": -73.99})
    assert h.call(h.rt.store.pop_trail_points, 10)[0]["source"] == "status"


# --- chat ------------------------------------------------------------------------------


def test_chat_post_is_idempotent_and_listed(h: RtHarness, rt_token: TokenFactory, pushes: list) -> None:
    driver, rider = rt_token(DRIVER), rt_token(RIDER)
    url = f"/rt/v1/rides/{LIVE_RIDE}/messages"
    first = h.client.post(url, json={"body": "I'm outside", "client_msg_id": "c-1"}, headers=bearer(driver))
    again = h.client.post(url, json={"body": "I'm outside", "client_msg_id": "c-1"}, headers=bearer(driver))
    assert first.status_code == 201
    assert again.status_code == 200
    assert first.json()["message"]["id"] == again.json()["message"]["id"]
    history = h.client.get(url, headers=bearer(rider)).json()["messages"]
    assert [m["body"] for m in history] == ["I'm outside"]
    assert history[0]["from"] == "driver"
    assert h.client.get(f"{url}?after_id={history[0]['id']}", headers=bearer(rider)).json()["messages"] == []
    assert h.client.get(url, headers=bearer(rt_token(STRANGER))).status_code == 403


def test_chat_push_carries_legacy_keys(h: RtHarness, rt_token: TokenFactory, pushes: list) -> None:
    register(h, rt_token(RIDER), "rider-token-0000000000000001")
    url = f"/rt/v1/rides/{LIVE_RIDE}/messages"
    h.client.post(url, json={"body": "I'm outside", "client_msg_id": "c-1"}, headers=bearer(rt_token(DRIVER)))
    h.drain()
    data = next(m.data for t, m in pushes if t == "rider-token-0000000000000001")
    assert (data["kind"], data["type"], data["bookingId"]) == ("chat", "chat_message", str(LIVE_RIDE))


# --- devices, share, location, flags --------------------------------------------------------


def test_device_moves_to_new_user_on_relogin(h: RtHarness, rt_token: TokenFactory) -> None:
    register(h, rt_token(RIDER), "shared-phone-token-000000001")
    register(h, rt_token(STRANGER), "shared-phone-token-000000001")
    assert h.sql("SELECT user_id FROM tbl_rt_user_devices") == [(STRANGER,)]
    r = h.client.post(
        "/rt/v1/devices/revoke",
        json={"token": "shared-phone-token-000000001"},
        headers=bearer(rt_token(STRANGER)),
    )
    assert r.status_code == 204
    assert h.sql("SELECT revoked_at IS NOT NULL FROM tbl_rt_user_devices") == [(1,)]


def test_only_passenger_can_create_share_link(h: RtHarness, rt_token: TokenFactory) -> None:
    assert (
        h.client.post(f"/rt/v1/rides/{LIVE_RIDE}/share", headers=bearer(rt_token(DRIVER))).status_code == 403
    )
    r = h.client.post(f"/rt/v1/rides/{LIVE_RIDE}/share", headers=bearer(rt_token(RIDER)))
    assert r.status_code == 200
    assert r.json()["expires_at"] > 0


def test_rest_location_fallback(h: RtHarness, rt_token: TokenFactory) -> None:
    rider = rt_token(RIDER)
    url = f"/rt/v1/rides/{LIVE_RIDE}/location"
    assert h.client.get(url, headers=bearer(rider)).json()["location"] is None
    with h.client.websocket_connect("/rt/v1/ws") as ws:
        ws.send_json(hello(rt_token(DRIVER), app="driver"))
        receive_until(ws, "welcome")
        ws.send_json(loc(LIVE_RIDE, seq=1, lat=40.7444))
        ws.send_json({"v": 1, "type": "ping"})
        receive_until(ws, "pong")
    body = h.client.get(url, headers=bearer(rider)).json()
    assert body["location"]["lat"] == 40.7444
    assert body["status"] == "en_route_pu"
    assert h.client.get(url, headers=bearer(rt_token(STRANGER))).status_code == 403


def test_flags_admin_only_and_validated(h: RtHarness, rt_token: TokenFactory) -> None:
    url = "/rt/v1/admin/flags/rt.provider"
    assert h.client.put(url, json={"value": "python"}, headers=bearer(rt_token(RIDER))).status_code == 403
    assert h.client.put(url, json={"value": "mqtt"}, headers=bearer(rt_token(ADMIN))).status_code == 422
    assert h.client.put(url, json={"value": "python"}, headers=bearer(rt_token(ADMIN))).status_code == 200
    assert h.client.get("/rt/v1/config").json()["rt"]["provider"] == "python"
    assert h.sql("SELECT name, updated_by_user_id FROM tbl_rt_settings") == [("rt.provider", ADMIN)]


def test_admin_broadcast_uses_topics(h: RtHarness, rt_token: TokenFactory, pushes: list) -> None:
    r = h.client.post(
        "/rt/v1/admin/broadcast",
        json={"audience": "all", "title": "Service update", "body": "Hello"},
        headers=bearer(rt_token(ADMIN)),
    )
    assert r.status_code == 200
    assert [t for t, _ in pushes] == ["/topics/staging_all_drivers", "/topics/staging_all_riders"]


# --- internal API (Laravel) ----------------------------------------------------------------

INTERNAL = {"x-secret": INTERNAL_SECRET}


def test_internal_routes_need_the_secret(h: RtHarness) -> None:
    body = {"userId": RIDER, "title": "t", "message": "m"}
    assert h.client.post("/api/notifications/send", json=body).status_code == 401
    assert (
        h.client.post("/api/notifications/send", json=body, headers={"x-secret": "wrong"}).status_code == 401
    )
    assert h.client.get("/api/health").status_code == 200  # Laravel's probe sends no secret


def test_legacy_send_is_accepted_and_delivered(h: RtHarness, rt_token: TokenFactory, pushes: list) -> None:
    register(h, rt_token(RIDER), "rider-token-0000000000000009")
    r = h.client.post(
        "/api/notifications/send",
        json={
            "userId": str(RIDER),
            "title": "Reminder",
            "message": "Pickup in 1 hour",
            "data": {"bookingId": 100},
        },
        headers=INTERNAL,
    )
    assert r.status_code == 202
    assert r.json()["success"] is True
    h.drain()
    assert pushes[0][1].title == "Reminder"


def test_legacy_per_user_topic_is_refused(h: RtHarness, pushes: list) -> None:
    body = {"topic": str(RIDER), "title": "t", "body": "b"}
    assert h.client.post("/api/notifications/fcm-topic", json=body, headers=INTERNAL).status_code == 422
    ok = h.client.post("/api/notifications/fcm-topic", json={**body, "topic": "all_riders"}, headers=INTERNAL)
    assert ok.status_code == 200
    assert pushes[0][0] == "/topics/staging_all_riders"


def test_internal_cancel_notifies_and_ends_ride(
    h: RtHarness, rt_token: TokenFactory, pushes: list, laravel: Any
) -> None:
    with h.client.websocket_connect("/rt/v1/ws") as ws:
        ws.send_json(hello(rt_token(RIDER)))
        receive_until(ws, "welcome")
        ws.send_json({"v": 1, "type": "subscribe", "ride_id": str(LIVE_RIDE)})
        receive_until(ws, "subscribed")
        h.sql("UPDATE tbl_reservations SET booking_status='cancelled' WHERE id=?", (LIVE_RIDE,))
        r = h.client.post(
            "/internal/v1/ride-events", json={"ride_id": LIVE_RIDE, "kind": "cancelled"}, headers=INTERNAL
        )
        assert r.status_code == 202
        frame = receive_until(ws, "ride.status")
    assert frame["data"]["status"] == "cancelled"
    assert frame["data"]["cancelled_by"] == "admin"
    assert frame["data"]["prev"] is None  # no watcher snapshot for this ride yet
    h.drain()
    assert h.call(h.rt.redis.ttl, f"rt:ride:{LIVE_RIDE}:events") <= h.rt.settings.rt_stream_ttl_ended_seconds


def _cancel(h: RtHarness, by: str) -> Any:
    h.sql("UPDATE tbl_reservations SET booking_status='cancelled' WHERE id=?", (LIVE_RIDE,))
    return h.client.post(
        "/internal/v1/ride-events",
        json={"ride_id": LIVE_RIDE, "kind": "cancelled", "by": by},
        headers=INTERNAL,
    )


def test_rider_cancel_tells_the_driver_only(
    h: RtHarness, rt_token: TokenFactory, pushes: list, laravel: Any
) -> None:
    register(h, rt_token(RIDER), "rider-token-0000000000000001")
    register(h, rt_token(DRIVER), "driver-token-000000000000001", app="driver")
    assert _cancel(h, "rider").status_code == 202
    h.drain()
    assert [t for t, _ in pushes] == ["driver-token-000000000000001"]
    push = pushes[0][1]
    assert push.title == "Ride cancelled"
    assert push.body == "The customer has cancelled this ride."
    assert push.data["cancelled_by"] == "rider"
    assert push.data["status"] == "cancelled"
    assert push.data["bookingId"] == str(LIVE_RIDE)


def test_driver_cancel_tells_the_rider_only(
    h: RtHarness, rt_token: TokenFactory, pushes: list, laravel: Any
) -> None:
    register(h, rt_token(RIDER), "rider-token-0000000000000001")
    register(h, rt_token(DRIVER), "driver-token-000000000000001", app="driver")
    assert _cancel(h, "driver").status_code == 202
    h.drain()
    assert [t for t, _ in pushes] == ["rider-token-0000000000000001"]
    assert pushes[0][1].body == "Your driver has cancelled this ride. We're sorry for the inconvenience."


def test_cancel_is_announced_once(h: RtHarness, rt_token: TokenFactory, pushes: list, laravel: Any) -> None:
    register(h, rt_token(DRIVER), "driver-token-000000000000001", app="driver")
    with h.client.websocket_connect("/rt/v1/ws") as ws:
        ws.send_json(hello(rt_token(RIDER)))
        receive_until(ws, "welcome")
        ws.send_json({"v": 1, "type": "subscribe", "ride_id": str(LIVE_RIDE)})
        receive_until(ws, "subscribed")
        assert _cancel(h, "rider").status_code == 202
        assert _cancel(h, "admin").status_code == 202  # e.g. a retried call
        frame = receive_until(ws, "ride.status")
    h.drain()
    assert frame["data"]["cancelled_by"] == "rider"
    assert h.call(h.rt.store.current_seq, LIVE_RIDE) == 1
    assert len(pushes) == 1


def test_internal_driver_change_drops_old_location(
    h: RtHarness, rt_token: TokenFactory, pushes: list
) -> None:
    with h.client.websocket_connect("/rt/v1/ws") as ws:
        ws.send_json(hello(rt_token(DRIVER), app="driver"))
        receive_until(ws, "welcome")
        ws.send_json(loc(LIVE_RIDE, seq=1))
        ws.send_json({"v": 1, "type": "ping"})
        receive_until(ws, "pong")
    h.sql("UPDATE tbl_reservations SET affiliate_id=6 WHERE id=?", (LIVE_RIDE,))
    r = h.client.post(
        "/internal/v1/ride-events",
        json={"ride_id": LIVE_RIDE, "kind": "driver_changed", "previous_driver_user_id": DRIVER},
        headers=INTERNAL,
    )
    assert r.status_code == 202
    assert h.call(h.rt.store.get_location, LIVE_RIDE) is None
    assert status(h, rt_token(DRIVER), "on_location").status_code == 403  # old driver is out
    assert status(h, rt_token(OTHER_DRIVER), "on_location").status_code == 200


# --- jobs ------------------------------------------------------------------------------------


def test_trail_sampling_and_flush(rt_factory: Any, rt_token: TokenFactory) -> None:
    from app.realtime.jobs import Jobs

    h = rt_factory(rt_location_max_per_second=100)  # the test sends faster than a phone

    with h.client.websocket_connect("/rt/v1/ws") as ws:
        ws.send_json(hello(rt_token(DRIVER), app="driver"))
        receive_until(ws, "welcome")
        base = 1_790_000_000_000
        # 1 s apart and ~1 m apart: only the 1st and the one 10 s later are kept.
        for i in range(12):
            ws.send_json(loc(LIVE_RIDE, seq=i + 1, lat=40.74 + i * 0.00001, ts=base + i * 1000))
            ws.send_json({"v": 1, "type": "ping"})
            receive_until(ws, "pong")
    written = h.call(Jobs(h.rt).flush_trail)
    assert written == 2
    assert h.sql("SELECT reservation_id, source FROM tbl_rt_location_points") == [
        (LIVE_RIDE, "nav_snapped"),
        (LIVE_RIDE, "nav_snapped"),
    ]


def test_proximity_alerts_fire_once_per_threshold(h: RtHarness, rt_token: TokenFactory, pushes: list) -> None:
    from app.realtime.jobs import Jobs

    register(h, rt_token(RIDER), "rider-token-0000000000000077")
    jobs = Jobs(h.rt)
    with h.client.websocket_connect("/rt/v1/ws") as ws:
        ws.send_json(hello(rt_token(DRIVER), app="driver"))
        receive_until(ws, "welcome")
        # Pickup is (40.75, -73.99). ~0.8 mi south, then ~0.1 mi south.
        for seq, lat in ((1, 40.7385), (2, 40.7385), (3, 40.7486)):
            ws.send_json(loc(LIVE_RIDE, seq=seq, lat=lat, lng=-73.99))
            ws.send_json({"v": 1, "type": "ping"})
            receive_until(ws, "pong")
            h.call(jobs.proximity)
    h.drain()
    titles = [m.title for _, m in pushes]
    assert titles == ["Your chauffeur is about a mile away", "Your chauffeur is almost there"]


def test_next_target_walks_charter_stops(h: RtHarness) -> None:
    from app.realtime.jobs import next_target

    info = h.call(h.rt.rides.get, CHARTER_RIDE)
    leg, point = next_target(info)
    assert leg == "stop0"
    assert (point.lat, point.lng) == (40.76, -73.98)
