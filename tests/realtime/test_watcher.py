"""The reservation watcher: Laravel-side cancels, reassignments and status edits reach
the apps without Laravel calling limo-rt."""

from __future__ import annotations

from typing import Any

import pytest

from app.realtime.fcm import PushMessage, SendOutcome
from tests.conftest import TokenFactory
from tests.realtime.conftest import (
    DRIVER,
    LIVE_RIDE,
    OTHER_DRIVER,
    RIDER,
    RtHarness,
    bearer,
    hello,
    loc,
    receive_until,
)


@pytest.fixture
def pushes(h: RtHarness) -> list[tuple[str, PushMessage]]:
    sent: list[tuple[str, PushMessage]] = []

    async def to_token(token: str, message: PushMessage) -> SendOutcome:
        sent.append((token, message))
        return SendOutcome.SENT

    h.rt.fcm.send_to_token = to_token  # type: ignore[method-assign]
    return sent


def watcher(h: RtHarness) -> Any:
    from app.realtime.jobs import Jobs

    return Jobs(h.rt).watcher


def laravel_update(h: RtHarness, sql: str, touch: bool = True) -> None:
    """What a Laravel controller does: change the row (and usually updated_at)."""
    stamp = ", updated_at = datetime('now')" if touch else ""
    h.sql(f"UPDATE tbl_reservations SET {sql}{stamp} WHERE id = ?", (LIVE_RIDE,))  # noqa: S608 - test literals


def subscribe(ws: Any, token: str) -> None:
    ws.send_json(hello(token))
    receive_until(ws, "welcome")
    ws.send_json({"v": 1, "type": "subscribe", "ride_id": str(LIVE_RIDE)})
    receive_until(ws, "subscribed")


def test_first_sight_only_takes_a_snapshot(h: RtHarness) -> None:
    w = watcher(h)
    assert h.call(w.sweep_open) == 0
    assert h.call(h.rt.store.current_seq, LIVE_RIDE) == 0
    assert h.call(h.rt.store.get_snapshot, LIVE_RIDE)["status"] == "en_route_pu"


def test_admin_cancel_reaches_rider(h: RtHarness, rt_token: TokenFactory, pushes: list) -> None:
    w = watcher(h)
    h.call(w.sweep_open)
    with h.client.websocket_connect("/rt/v1/ws") as ws:
        subscribe(ws, rt_token(RIDER))
        laravel_update(h, "booking_status = 'cancelled'")
        assert h.call(w.scan_recent) == 1
        frame = receive_until(ws, "ride.status")
    assert frame["data"]["status"] == "cancelled"
    assert frame["data"]["cancelled_by"] == "admin"
    assert h.call(w.scan_recent) == 0  # reported once


def test_watcher_does_not_repeat_an_explicit_cancel(
    h: RtHarness, rt_token: TokenFactory, pushes: list
) -> None:
    w = watcher(h)
    h.call(w.sweep_open)
    laravel_update(h, "booking_status = 'cancelled'")
    r = h.client.post(
        "/internal/v1/ride-events",
        json={"ride_id": LIVE_RIDE, "kind": "cancelled", "by": "rider"},
        headers={"x-secret": h.rt.settings.rt_internal_secret.get_secret_value()},
    )
    assert r.status_code == 202
    assert h.call(w.scan_recent) == 0  # the snapshot already says cancelled
    assert h.call(h.rt.store.current_seq, LIVE_RIDE) == 1
    events = h.call(h.rt.store.replay, LIVE_RIDE, 0)[0]
    assert '"prev":"en_route_pu"' in events[0]
    assert '"cancelled_by":"rider"' in events[0]


def test_reassignment_drops_old_driver(h: RtHarness, rt_token: TokenFactory, pushes: list) -> None:
    w = watcher(h)
    h.call(w.sweep_open)
    driver_token = rt_token(DRIVER)
    h.client.post(
        "/rt/v1/devices",
        json={"token": "old-driver-token-000000000001", "app": "driver", "platform": "ios"},
        headers=bearer(driver_token),
    )
    with h.client.websocket_connect("/rt/v1/ws") as ws:
        ws.send_json(hello(driver_token, app="driver"))
        receive_until(ws, "welcome")
        ws.send_json(loc(LIVE_RIDE, seq=1))
        ws.send_json({"v": 1, "type": "ping"})
        receive_until(ws, "pong")
    with h.client.websocket_connect("/rt/v1/ws") as rider:
        subscribe(rider, rt_token(RIDER))
        laravel_update(h, "affiliate_id = 6, driver_id = 2")
        h.call(w.scan_recent)
        frame = receive_until(rider, "ride.driver_changed")
    h.drain()
    assert frame["data"]["driver_id"] == str(OTHER_DRIVER)
    assert h.call(h.rt.store.get_location, LIVE_RIDE) is None
    assert ("old-driver-token-000000000001", "Ride reassigned") in [(t, m.title) for t, m in pushes]
    r = h.client.post(
        f"/rt/v1/driver/rides/{LIVE_RIDE}/status",
        json={"status": "on_location"},
        headers=bearer(rt_token(DRIVER)),
    )
    assert r.status_code == 403


def test_own_status_writes_are_not_echoed(h: RtHarness, rt_token: TokenFactory, pushes: list) -> None:
    w = watcher(h)
    h.call(w.sweep_open)
    r = h.client.post(
        f"/rt/v1/driver/rides/{LIVE_RIDE}/status",
        json={"status": "on_location"},
        headers=bearer(rt_token(DRIVER)),
    )
    assert r.status_code == 200
    laravel_update(h, "booking_status = booking_status")  # same row shows up as recently updated
    assert h.call(w.scan_recent) == 0
    assert h.call(h.rt.store.current_seq, LIVE_RIDE) == 1


def test_admin_status_edit_is_broadcast(h: RtHarness, rt_token: TokenFactory, pushes: list) -> None:
    w = watcher(h)
    h.call(w.sweep_open)
    with h.client.websocket_connect("/rt/v1/ws") as ws:
        subscribe(ws, rt_token(RIDER))
        laravel_update(h, "booking_status = 'ended'")
        h.call(w.scan_recent)
        frame = receive_until(ws, "ride.status")
    assert frame["data"]["status"] == "ended"
    assert frame["data"]["prev"] is None


def test_sweep_catches_writes_that_skip_updated_at(h: RtHarness, pushes: list) -> None:
    w = watcher(h)
    h.call(w.sweep_open)
    laravel_update(h, "booking_status = 'on_location'", touch=False)
    assert h.call(w.scan_recent) == 0  # updated_at didn't move
    assert h.call(w.sweep_open) == 1
