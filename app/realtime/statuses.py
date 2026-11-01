"""Booking status vocabulary, ported from Laravel's BookingTrackingService.

Keep in step with ``limoapi/app/Services/Mobile/Booking/BookingTrackingService.php``;
tests/realtime/test_statuses.py reads the PHP constants and fails if they drift.
"""

from __future__ import annotations

PENDING = "pending"
ASSIGNED = "assigned"
REJECTED = "rejected"
CANCELLED = "cancelled"
EN_ROUTE_PU = "en_route_pu"
ON_LOCATION = "on_location"
EN_ROUTE_DO = "en_route_do"
ENDED = "ended"
FINALIZED = "finalized"
START_RIDE = "start_ride"
PASSENGER_DROP = "passenger_drop"
PASSENGER_PICK = "passenger_pick"

# Statuses that carry a stop_info block.
STOP_STATUSES = frozenset({PASSENGER_DROP, PASSENGER_PICK})

# A driver is currently working the booking.
LIVE_STATUSES = frozenset({EN_ROUTE_PU, ON_LOCATION, START_RIDE, PASSENGER_DROP, PASSENGER_PICK, EN_ROUTE_DO})

# The passenger is aboard (or being served at a stop).
RIDE_STARTED_STATUSES = frozenset({START_RIDE, PASSENGER_DROP, PASSENGER_PICK, EN_ROUTE_DO})

ALL_STATUSES = frozenset(
    {
        PENDING,
        ASSIGNED,
        REJECTED,
        CANCELLED,
        EN_ROUTE_PU,
        ON_LOCATION,
        START_RIDE,
        PASSENGER_DROP,
        PASSENGER_PICK,
        EN_ROUTE_DO,
        ENDED,
        FINALIZED,
    }
)

# The ride is over: share links stop working and the event stream gets its short TTL.
TERMINAL_STATUSES = frozenset({ENDED, FINALIZED, CANCELLED, REJECTED})

# A driver status update may set these. The rest are set by Laravel (booking, admin).
DRIVER_SETTABLE = frozenset(
    {EN_ROUTE_PU, ON_LOCATION, START_RIDE, PASSENGER_DROP, PASSENGER_PICK, EN_ROUTE_DO, ENDED}
)

# How far back a (booking, status, stop_index, event_at) match counts as a duplicate.
DEDUP_WINDOW_MINUTES = 30
