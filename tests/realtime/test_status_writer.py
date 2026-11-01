"""Parity with Laravel's BookingTrackingService::recordStatusUpdate."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from app.realtime import statuses
from app.realtime.status_writer import StatusContext, parse_event_time
from tests.realtime.conftest import CHARTER_RIDE, DRIVER, ENDED_RIDE, LIVE_RIDE, RtHarness

PHP_SERVICE = Path(__file__).resolve().parents[3] / (
    "limoapi/app/Services/Mobile/Booking/BookingTrackingService.php"
)


def record(h: RtHarness, ride_id: int, status: str, **ctx: object):
    return h.call(h.rt.writer.record, ride_id, status, StatusContext(driver_user_id=DRIVER, **ctx))


def test_applies_status_and_writes_history(h: RtHarness) -> None:
    result = record(h, LIVE_RIDE, "on_location", timestamp="2026-10-31T10:05:00Z", idempotency_key="k1")
    assert result.updated
    assert not result.duplicate
    assert not result.stale
    assert result.previous_status == "en_route_pu"
    row = h.sql("SELECT booking_status, status_change_flag FROM tbl_reservations WHERE id=?", (LIVE_RIDE,))
    assert row == [("on_location", "yes")]
    history = h.sql(
        "SELECT previous_status, booking_status, substr(event_at, 1, 19), idempotency_key, driver_id "
        "FROM tbl_booking_status_history WHERE reservation_id=?",
        (LIVE_RIDE,),
    )
    assert history == [("en_route_pu", "on_location", "2026-10-31 10:05:00", "k1", DRIVER)]


def test_same_idempotency_key_is_a_duplicate(h: RtHarness) -> None:
    record(h, LIVE_RIDE, "on_location", idempotency_key="same")
    again = record(h, LIVE_RIDE, "start_ride", idempotency_key="same")
    assert again.duplicate
    assert not again.updated
    assert h.sql("SELECT booking_status FROM tbl_reservations WHERE id=?", (LIVE_RIDE,)) == [("on_location",)]
    assert len(h.sql("SELECT id FROM tbl_booking_status_history")) == 1


def test_same_event_time_is_a_duplicate_without_a_key(h: RtHarness) -> None:
    record(h, LIVE_RIDE, "on_location", timestamp=1_793_448_300_000)
    again = record(h, LIVE_RIDE, "on_location", timestamp=1_793_448_300_000)
    assert again.duplicate


def test_older_event_is_stale_and_history_only(h: RtHarness) -> None:
    record(h, LIVE_RIDE, "start_ride", timestamp="2026-10-31T10:10:00Z")
    late = record(h, LIVE_RIDE, "on_location", timestamp="2026-10-31T10:05:00Z")
    assert late.stale
    assert not late.updated
    assert late.status == "start_ride"
    assert h.sql("SELECT booking_status FROM tbl_reservations WHERE id=?", (LIVE_RIDE,)) == [("start_ride",)]
    assert len(h.sql("SELECT id FROM tbl_booking_status_history")) == 2


def test_stop_status_sets_stop_columns(h: RtHarness) -> None:
    result = record(
        h,
        CHARTER_RIDE,
        "passenger_drop",
        timestamp="2026-10-31T10:20:00Z",
        stop_info={"index": 1, "address": "Stop B", "latitude": 40.78, "longitude": 0},
    )
    assert result.updated
    assert result.stop_info == {
        "index": 1,
        "stop_number": 2,
        "total_stops": 2,
        "address": "Stop B",
        "latitude": 40.78,
        "longitude": None,  # zero means "unknown", like Laravel
    }
    row = h.sql(
        "SELECT current_stop_index, current_stop_status, total_stops, substr(stop_status_updated_at, 1, 19) "
        "FROM tbl_reservations WHERE id=?",
        (CHARTER_RIDE,),
    )
    assert row == [(1, "passenger_drop", 2, "2026-10-31 10:20:00")]


def test_start_ride_counts_stops_and_pickup_leg_resets_cursor(h: RtHarness) -> None:
    record(h, CHARTER_RIDE, "passenger_drop", stop_info={"index": 0})
    record(h, CHARTER_RIDE, "start_ride")
    assert h.sql(
        "SELECT current_stop_index, total_stops FROM tbl_reservations WHERE id=?", (CHARTER_RIDE,)
    ) == [(None, 2)]
    record(h, CHARTER_RIDE, "passenger_drop", stop_info={"index": 0})
    record(h, CHARTER_RIDE, "en_route_pu")
    row = h.sql(
        "SELECT current_stop_index, current_stop_status, stop_status_updated_at "
        "FROM tbl_reservations WHERE id=?",
        (CHARTER_RIDE,),
    )
    assert row == [(None, None, None)]


def test_stop_status_without_stop_info_is_a_plain_write(h: RtHarness) -> None:
    result = record(h, CHARTER_RIDE, "passenger_drop", stop_info={"address": "no index"})
    assert result.updated
    assert result.stop_info is None


def test_completed_transition_only_on_real_end(h: RtHarness) -> None:
    assert record(h, LIVE_RIDE, "ended").completed_transition
    assert not record(h, LIVE_RIDE, "ended").completed_transition  # re-end
    assert not record(h, ENDED_RIDE, "ended").completed_transition


def test_invalid_status_and_missing_ride(h: RtHarness) -> None:
    bad = record(h, LIVE_RIDE, "teleported")
    assert not bad.accepted
    assert "Invalid booking status" in (bad.message or "")
    assert not record(h, 999, "on_location").found


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-31T10:05:00Z", datetime(2026, 10, 31, 10, 5)),
        ("2026-10-31T15:35:00.250+05:30", datetime(2026, 10, 31, 10, 5)),
        ("2026-10-31 10:05:00", datetime(2026, 10, 31, 10, 5)),
        (1_793_441_100_000, datetime(2026, 10, 31, 10, 5)),
        ("1793441100", datetime(2026, 10, 31, 10, 5)),
        ("not a date", None),
        (None, None),
    ],
)
def test_parse_event_time(raw: object, expected: datetime | None) -> None:
    assert parse_event_time(raw) == expected


@pytest.mark.skipif(not PHP_SERVICE.exists(), reason="limoapi checkout not next to limoagent")
def test_status_vocabulary_matches_laravel() -> None:
    source = PHP_SERVICE.read_text(encoding="utf-8")
    php = dict(re.findall(r"const STATUS_([A-Z_]+) = '([a-z_]+)';", source))
    assert set(php.values()) == statuses.ALL_STATUSES

    def block(name: str) -> set[str]:
        body = re.search(rf"const {name} = \[(.*?)\];", source, re.S)
        assert body is not None
        return {php[n] for n in re.findall(r"self::STATUS_([A-Z_]+)", body.group(1))}

    assert block("LIVE_STATUSES") == statuses.LIVE_STATUSES
    assert block("STOP_STATUSES") == statuses.STOP_STATUSES
    assert block("RIDE_STARTED_STATUSES") == statuses.RIDE_STARTED_STATUSES
    assert f"STATUS_DEDUP_WINDOW_MINUTES = {statuses.DEDUP_WINDOW_MINUTES};" in source
