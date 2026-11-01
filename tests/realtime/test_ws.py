"""WebSocket protocol: auth, ride access, live location, replay, share links."""

from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.conftest import TokenFactory
from tests.realtime.conftest import (
    AGENT_PAX,
    DRIVER,
    ENDED_RIDE,
    LIVE_RIDE,
    OTHER_DRIVER,
    OTHER_RIDE,
    RIDER,
    STRANGER,
    RtHarness,
    bearer,
    hello,
    loc,
    receive_until,
)

WS = "/rt/v1/ws"


def test_hello_is_required(h: RtHarness) -> None:
    with h.client.websocket_connect(WS) as ws:
        ws.send_json({"v": 1, "type": "subscribe", "ride_id": "100"})
        assert ws.receive_json()["data"]["code"] == "hello_required"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4400


def test_bad_token_is_rejected(h: RtHarness, rt_token: TokenFactory) -> None:
    with h.client.websocket_connect(WS) as ws:
        ws.send_json(hello(rt_token(RIDER, revoked=True)))
        assert ws.receive_json()["data"]["code"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401


def test_welcome_has_server_time_and_active_ride(h: RtHarness, rt_token: TokenFactory) -> None:
    with h.client.websocket_connect(WS) as ws:
        ws.send_json(hello(rt_token(DRIVER), app="driver"))
        welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    data = welcome["data"]
    assert data["role"] == "driver"
    assert data["user_id"] == str(DRIVER)
    assert data["active_ride"] == {"ride_id": str(LIVE_RIDE), "status": "en_route_pu"}  # newest live ride
    assert data["server_time"] > 0


def test_travel_agent_passenger_can_watch(h: RtHarness, rt_token: TokenFactory) -> None:
    with h.client.websocket_connect(WS) as ws:
        ws.send_json(hello(rt_token(AGENT_PAX)))
        receive_until(ws, "welcome")
        ws.send_json({"v": 1, "type": "subscribe", "ride_id": str(LIVE_RIDE)})
        assert receive_until(ws, "subscribed")["data"]["status"] == "en_route_pu"


@pytest.mark.parametrize("user", [STRANGER, OTHER_DRIVER])
def test_strangers_cannot_subscribe(h: RtHarness, rt_token: TokenFactory, user: int) -> None:
    with h.client.websocket_connect(WS) as ws:
        ws.send_json(hello(rt_token(user)))
        receive_until(ws, "welcome")
        ws.send_json({"v": 1, "type": "subscribe", "ride_id": str(LIVE_RIDE)})
        frame = ws.receive_json()
    assert frame["type"] == "error"
    assert frame["data"]["code"] == "forbidden_ride"


def test_driver_location_reaches_rider_in_order(h: RtHarness, rt_token: TokenFactory) -> None:
    with h.client.websocket_connect(WS) as rider, h.client.websocket_connect(WS) as driver:
        rider.send_json(hello(rt_token(RIDER)))
        receive_until(rider, "welcome")
        rider.send_json({"v": 1, "type": "subscribe", "ride_id": str(LIVE_RIDE)})
        receive_until(rider, "subscribed")

        driver.send_json(hello(rt_token(DRIVER), app="driver"))
        receive_until(driver, "welcome")
        driver.send_json(loc(LIVE_RIDE, seq=5, lat=40.741))
        driver.send_json(loc(LIVE_RIDE, seq=4, lat=40.999))  # older: dropped
        driver.send_json(loc(LIVE_RIDE, seq=6, lat=40.742))

        first = receive_until(rider, "loc")
        second = receive_until(rider, "loc")
    assert [first["data"]["seq"], second["data"]["seq"]] == [5, 6]
    assert first["data"]["lat"] == 40.741
    assert first["data"]["snapshot"] is False
    assert first["seq"] is None  # location is latest-wins, never replayed


def test_only_the_assigned_driver_can_send_location(h: RtHarness, rt_token: TokenFactory) -> None:
    with h.client.websocket_connect(WS) as ws:
        ws.send_json(hello(rt_token(OTHER_DRIVER), app="driver"))
        receive_until(ws, "welcome")
        ws.send_json(loc(LIVE_RIDE, seq=1))
        frame = ws.receive_json()
    assert frame["data"]["code"] == "forbidden_ride"
    assert h.call(h.rt.store.get_location, LIVE_RIDE) is None


def test_riders_cannot_send_location(h: RtHarness, rt_token: TokenFactory) -> None:
    with h.client.websocket_connect(WS) as ws:
        ws.send_json(hello(rt_token(RIDER)))
        receive_until(ws, "welcome")
        ws.send_json(loc(LIVE_RIDE, seq=1))
        assert ws.receive_json()["data"]["code"] == "forbidden_ride"


def test_subscribe_replays_missed_events_then_location_snapshot(h: RtHarness, rt_token: TokenFactory) -> None:
    driver_token = rt_token(DRIVER)
    for status in ("on_location", "start_ride"):
        r = h.client.post(
            f"/rt/v1/driver/rides/{LIVE_RIDE}/status", json={"status": status}, headers=bearer(driver_token)
        )
        assert r.status_code == 200, r.text
    with h.client.websocket_connect(WS) as driver:
        driver.send_json(hello(driver_token, app="driver"))
        receive_until(driver, "welcome")
        driver.send_json(loc(LIVE_RIDE, seq=1))
        driver.send_json({"v": 1, "type": "ping"})
        receive_until(driver, "pong")  # the fix has been processed

    with h.client.websocket_connect(WS) as rider:
        rider.send_json(hello(rt_token(RIDER)))
        receive_until(rider, "welcome")
        rider.send_json({"v": 1, "type": "subscribe", "ride_id": str(LIVE_RIDE), "data": {"last_seq": 1}})
        replayed = rider.receive_json()
        snapshot = rider.receive_json()
        subscribed = rider.receive_json()
    assert replayed["type"] == "ride.status"
    assert replayed["seq"] == 2
    assert replayed["data"]["status"] == "start_ride"
    assert replayed["data"]["prev"] == "on_location"
    assert snapshot["type"] == "loc"
    assert snapshot["data"]["snapshot"] is True
    assert subscribed["type"] == "subscribed"
    assert subscribed["data"]["seq"] == 2


def test_replay_gap_asks_for_resync(h: RtHarness, rt_token: TokenFactory) -> None:
    for i in range(5):
        h.call(h.rt.store.append_event, LIVE_RIDE, "ride.notice", {"i": i})
    h.call(h.rt.redis.xtrim, f"rt:ride:{LIVE_RIDE}:events", maxlen=2, approximate=False)
    with h.client.websocket_connect(WS) as ws:
        ws.send_json(hello(rt_token(RIDER)))
        receive_until(ws, "welcome")
        ws.send_json({"v": 1, "type": "subscribe", "ride_id": str(LIVE_RIDE), "data": {"last_seq": 1}})
        assert ws.receive_json()["type"] == "resync.required"


def test_share_link_viewer_gets_location_but_not_chat(h: RtHarness, rt_token: TokenFactory) -> None:
    share = h.client.post(f"/rt/v1/rides/{LIVE_RIDE}/share", headers=bearer(rt_token(RIDER))).json()["token"]
    with h.client.websocket_connect(WS) as viewer:
        viewer.send_json(hello(share, app="web"))
        welcome = receive_until(viewer, "welcome")
        assert welcome["data"]["role"] == "viewer"
        viewer.send_json({"v": 1, "type": "subscribe", "ride_id": str(LIVE_RIDE)})
        receive_until(viewer, "subscribed")

        h.client.post(
            f"/rt/v1/rides/{LIVE_RIDE}/messages", json={"body": "secret"}, headers=bearer(rt_token(DRIVER))
        )
        with h.client.websocket_connect(WS) as driver:
            driver.send_json(hello(rt_token(DRIVER), app="driver"))
            receive_until(driver, "welcome")
            driver.send_json(loc(LIVE_RIDE, seq=1))
            frame = viewer.receive_json()
    assert frame["type"] == "loc"  # the chat frame (published first) was filtered out

    with h.client.websocket_connect(WS) as other:
        other.send_json(hello(share, app="web"))
        receive_until(other, "welcome")
        other.send_json({"v": 1, "type": "subscribe", "ride_id": str(OTHER_RIDE)})
        assert other.receive_json()["data"]["code"] == "forbidden_ride"


def test_share_link_stops_when_ride_is_over(h: RtHarness, rt_token: TokenFactory) -> None:
    token, _ = h.rt.shares.mint(ENDED_RIDE)
    with h.client.websocket_connect(WS) as ws:
        ws.send_json(hello(token, app="web"))
        assert ws.receive_json()["data"]["code"] == "share_expired"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4410
    r = h.client.get(f"/rt/v1/rides/{ENDED_RIDE}/location", headers=bearer(token))
    assert r.status_code == 403


def test_presence_tracks_foreground_and_background(h: RtHarness, rt_token: TokenFactory) -> None:
    with h.client.websocket_connect(WS) as ws:
        ws.send_json(hello(rt_token(RIDER)))
        receive_until(ws, "welcome")
        assert h.call(h.rt.store.presence, RIDER) == "foreground"
        ws.send_json({"v": 1, "type": "app_state", "data": {"state": "background"}})
        ws.send_json({"v": 1, "type": "ping"})
        receive_until(ws, "pong")
        assert h.call(h.rt.store.presence, RIDER) == "background"
    assert h.call(h.rt.store.presence, RIDER) is None
