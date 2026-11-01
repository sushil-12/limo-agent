"""Fixtures for limo-rt: a SQLite stand-in for the Laravel tables it touches (plus its own
``tbl_rt_*`` tables), fakeredis, Passport-identical tokens and a TestClient.

SQLite ignores ``SELECT ... FOR UPDATE``; locking is covered by the MySQL integration run.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fakeredis
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy.ext.asyncio import create_async_engine

from app.realtime.main import create_app
from app.realtime.services import Realtime
from app.realtime.settings import RealtimeSettings
from app.realtime.tables import rt_metadata
from tests.conftest import TokenFactory

INTERNAL_SECRET = "i" * 40
SHARE_SECRET = "z" * 40

RIDER, DRIVER, ADMIN, STRANGER, OTHER_DRIVER, AGENT_PAX = 10, 11, 12, 15, 16, 17
LIVE_RIDE, CHARTER_RIDE, ENDED_RIDE, OTHER_RIDE = 100, 101, 102, 103

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT, role INTEGER NOT NULL,
                    soft_delete INTEGER DEFAULT 0);
CREATE TABLE tbl_account (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, first_name TEXT NOT NULL,
                          status TEXT NOT NULL DEFAULT 'enable', soft_delete INTEGER);
CREATE TABLE oauth_clients (id INTEGER PRIMARY KEY, provider TEXT, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE oauth_access_tokens (id TEXT PRIMARY KEY, user_id INTEGER, client_id INTEGER NOT NULL,
                                  revoked INTEGER NOT NULL DEFAULT 0, expires_at DATETIME);
CREATE TABLE tbl_reservations (
    id INTEGER PRIMARY KEY, acc_id INTEGER, travel_client_id INTEGER, affiliate_id INTEGER, driver_id INTEGER,
    booking_status TEXT, status_change_flag TEXT, service_type TEXT, extra_stops TEXT,
    pickup_latitude REAL, pickup_longitude REAL, dropoff_latitude REAL, dropoff_longitude REAL,
    current_stop_index INTEGER, current_stop_status TEXT, total_stops INTEGER,
    stop_status_updated_at DATETIME, updated_at DATETIME);
CREATE TABLE tbl_booking_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, reservation_id INTEGER NOT NULL, previous_status TEXT,
    booking_status TEXT NOT NULL, stop_index INTEGER, stop_number INTEGER, total_stops INTEGER,
    stop_address TEXT, stop_latitude REAL, stop_longitude REAL, driver_id INTEGER, customer_id INTEGER,
    latitude REAL, longitude REAL, event_at DATETIME, source TEXT, correlation_id TEXT,
    idempotency_key TEXT, metadata TEXT, created_at DATETIME, updated_at DATETIME);
"""

SEED = """
INSERT INTO users VALUES (10, 'Asha Rider', 'asha@example.com', 5, 0);
INSERT INTO users VALUES (11, 'Dev Driver', 'dev@example.com', 2, 0);
INSERT INTO users VALUES (12, 'Ada Admin', 'ada@example.com', 1, 0);
INSERT INTO users VALUES (15, 'Sam Stranger', 'sam@example.com', 5, 0);
INSERT INTO users VALUES (16, 'Ola Otherdriver', 'ola@example.com', 2, 0);
INSERT INTO users VALUES (17, 'Pia Passenger', 'pia@example.com', 5, 0);
INSERT INTO tbl_account (id, user_id, first_name) VALUES (1, 10, 'Asha'), (2, 11, 'Dev'), (3, 12, 'Ada'),
    (5, 15, 'Sam'), (6, 16, 'Ola'), (7, 17, 'Pia');
INSERT INTO oauth_clients VALUES (1, NULL, 0);
INSERT INTO tbl_reservations (id, acc_id, travel_client_id, affiliate_id, driver_id, booking_status,
    service_type, extra_stops, pickup_latitude, pickup_longitude, dropoff_latitude, dropoff_longitude,
    updated_at) VALUES
    (100, 1, 7, 2, 1, 'en_route_pu', 'oneway', NULL, 40.7500, -73.9900, 40.6400, -73.7800,
     '2026-10-31 10:00:00'),
    (101, 1, NULL, 2, 1, 'start_ride', 'chartertour',
     '[{"address":"Stop A","latitude":40.76,"longitude":-73.98},
       {"address":"Stop B","lat":40.78,"lng":-73.96}]',
     40.7500, -73.9900, 40.6400, -73.7800, '2026-10-31 09:00:00'),
    (102, 1, NULL, 2, 1, 'ended', 'oneway', NULL, 40.7500, -73.9900, 40.6400, -73.7800,
     '2026-10-30 09:00:00'),
    (103, 5, NULL, 6, 2, 'en_route_pu', 'oneway', NULL, 40.7500, -73.9900, 40.6400, -73.7800,
     '2026-10-31 08:00:00');
"""


@pytest.fixture
def rt_db_path(tmp_path: Path) -> Path:
    path = tmp_path / "rt.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA + SEED)
    rt_metadata.create_all(create_sync_engine(f"sqlite:///{path}"))
    return path


@pytest.fixture
def rt_token(rsa_private_key: rsa.RSAPrivateKey, rt_db_path: Path) -> TokenFactory:
    return TokenFactory(rsa_private_key, rt_db_path)


@pytest.fixture
def rt_settings_factory(public_pem: str) -> Callable[..., RealtimeSettings]:
    def build(**overrides: Any) -> RealtimeSettings:
        values: dict[str, Any] = {
            "app_env": "testing",
            "log_level": "WARNING",
            "db_database": "laravel",
            "db_username": "agent_ro",
            "rt_db_username": "limo_rt",
            "rt_db_password": "pw",
            "passport_public_key": public_pem,
            "rt_internal_secret": INTERNAL_SECRET,
            "rt_share_token_secret": SHARE_SECRET,
            "laravel_api_base_url": "http://laravel.test/api/",
            "rt_cors_origins": ["*"],
            "rt_push_ack_timeout_seconds": 0.2,
            # Jobs are driven by hand in tests.
            "rt_trail_flush_seconds": 3600,
            "rt_proximity_interval_seconds": 3600,
            "rt_flags_refresh_seconds": 3600,
            "rt_watch_interval_seconds": 3600,
            "rt_watch_sweep_seconds": 3600,
        }
        values.update(overrides)
        return RealtimeSettings(_env_file=None, **values)  # type: ignore[call-arg]

    return build


@dataclass
class RtHarness:
    client: TestClient
    redis: fakeredis.FakeAsyncRedis
    db_path: Path

    @property
    def rt(self) -> Realtime:
        return self.client.app.state.rt  # type: ignore[attr-defined,no-any-return]

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a coroutine function on the app's event loop."""
        return self.client.portal.call(lambda: fn(*args, **kwargs))  # type: ignore[union-attr]

    def drain(self) -> None:
        self.call(self.rt.tasks.drain, 5.0)

    def sql(self, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(query, params).fetchall()


@pytest.fixture
def rt_factory(
    rt_settings_factory: Callable[..., RealtimeSettings], rt_db_path: Path
) -> Iterator[Callable[..., RtHarness]]:
    clients: list[TestClient] = []

    def build(**overrides: Any) -> RtHarness:
        engine = create_async_engine(f"sqlite+aiosqlite:///{rt_db_path}")
        redis = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer())
        app = create_app(rt_settings_factory(**overrides), engine=engine, redis=redis)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return RtHarness(client, redis, rt_db_path)

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def h(rt_factory: Callable[..., RtHarness]) -> RtHarness:
    return rt_factory()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def hello(token: str, app: str = "rider") -> dict[str, Any]:
    return {"v": 1, "type": "hello", "data": {"token": token, "app": app, "platform": "ios"}}


def loc(
    ride_id: int, *, seq: int, lat: float = 40.7400, lng: float = -73.9950, epoch: str = "e1", ts: int = 0
):
    return {
        "v": 1,
        "type": "loc.update",
        "ride_id": str(ride_id),
        "data": {
            "lat": lat,
            "lng": lng,
            "acc_m": 5.0,
            "speed_mps": 10.0,
            "heading": 90.0,
            "gps_ts": ts or 1_790_000_000_000 + seq * 1000,
            "epoch": epoch,
            "seq": seq,
            "src": "nav_snapped",
        },
    }


def receive_until(ws: Any, type_: str, limit: int = 20) -> dict[str, Any]:
    """Read frames until one of ``type_`` arrives (others are skipped)."""
    for _ in range(limit):
        frame = ws.receive_json()
        if frame["type"] == type_:
            return frame  # type: ignore[no-any-return]
    raise AssertionError(f"no {type_!r} frame within {limit} frames")
