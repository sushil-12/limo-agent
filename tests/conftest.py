"""Shared fixtures: an RSA key pair, a seeded SQLite stand-in for the Laravel DB,
Passport-identical token minting, and a TestClient with Ollama mocked by respx.

The SQLite tables mirror only the columns the service reads. They exist purely for
tests; the service itself never creates tables.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.main import create_app

OLLAMA_URL = "http://ollama.test"
MODEL = "llama3.1:8b"
SERVICE_KEY = "s" * 40

RIDER_ID, DRIVER_ID, ADMIN_ID, DELETED_ID, DISABLED_ID = 10, 11, 12, 13, 14
CLIENT_OK, CLIENT_REVOKED, CLIENT_DRIVERS = 1, 2, 3

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT, role INTEGER NOT NULL,
                    soft_delete INTEGER DEFAULT 0);
CREATE TABLE tbl_account (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, first_name TEXT NOT NULL,
                          status TEXT NOT NULL DEFAULT 'enable', soft_delete INTEGER);
CREATE TABLE oauth_clients (id INTEGER PRIMARY KEY, provider TEXT, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE oauth_access_tokens (id TEXT PRIMARY KEY, user_id INTEGER, client_id INTEGER NOT NULL,
                                  revoked INTEGER NOT NULL DEFAULT 0, expires_at DATETIME);
CREATE TABLE tbl_driver_setting (id INTEGER PRIMARY KEY, acc_id INTEGER, first_name TEXT, otp TEXT);
CREATE TABLE tbl_reservations (id INTEGER PRIMARY KEY, conf_id TEXT, acc_id INTEGER, driver_id INTEGER,
                               affiliate_id INTEGER, booking_status TEXT, payment_status TEXT,
                               pickup_date DATE, quote_amount REAL, email_token TEXT, status TEXT,
                               travel_client_id INTEGER);
"""

SEED = """
INSERT INTO users VALUES (10, 'Asha Rider', 'asha@example.com', 5, 0);
INSERT INTO users VALUES (11, 'Dev Driver', 'dev@example.com', 2, 0);
INSERT INTO users VALUES (12, 'Ada Admin', 'ada@example.com', 1, 0);
INSERT INTO users VALUES (13, 'Gone User', 'gone@example.com', 5, 1);
INSERT INTO users VALUES (14, 'Off User', 'off@example.com', 5, 0);
INSERT INTO tbl_account (user_id, first_name, status) VALUES (10, 'Asha', 'enable');
INSERT INTO tbl_account (user_id, first_name, status) VALUES (11, 'Dev', 'enable');
INSERT INTO tbl_account (user_id, first_name, status) VALUES (14, 'Off', 'disable');
INSERT INTO oauth_clients VALUES (1, NULL, 0);
INSERT INTO oauth_clients VALUES (2, NULL, 1);
INSERT INTO oauth_clients VALUES (3, 'drivers', 0);
INSERT INTO tbl_driver_setting VALUES (1, 2, 'Dev', '9999');
INSERT INTO tbl_reservations VALUES
    (1, 'C-1001', 1, NULL, NULL, 'pending', 'unpaid', '2026-10-20', 120, 't1', 'enable', NULL),
    (2, 'C-1002', 1, NULL, NULL, 'pending', 'unpaid', '2026-10-22', 80, 't2', 'enable', NULL),
    (3, 'C-1003', 1, 1, NULL, 'ended', 'paid', '2026-09-01', 95, 't3', 'enable', NULL),
    (4, 'C-1004', 3, 1, NULL, 'pending', 'unpaid', '2026-10-25', 60, 't4', 'enable', NULL),
    (5, 'C-1005', 3, NULL, NULL, 'pending', 'unpaid', '2026-10-26', 70, 't5', 'disable', NULL);
"""


@pytest.fixture(scope="session")
def rsa_private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def public_pem(rsa_private_key: rsa.RSAPrivateKey) -> str:
    return (
        rsa_private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "laravel.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA + SEED)
    return path


def _db_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


@dataclass
class TokenFactory:
    """Mints tokens exactly like Passport (league/oauth2-server 8 + lcobucci/jwt 4)."""

    private_key: Any
    db_path: Path
    issued: list[str] = field(default_factory=list)

    def __call__(
        self,
        user_id: int = RIDER_ID,
        *,
        client_id: int = CLIENT_OK,
        jti: str | None = None,
        exp_in: float = 3600,
        nbf_in: float = 0,
        db_row: bool = True,
        db_user_id: int | None = None,
        db_client_id: int | None = None,
        db_expires_at: datetime | None = None,
        revoked: bool = False,
        key: Any = None,
        claims: dict[str, Any] | None = None,
    ) -> str:
        jti = jti or secrets.token_hex(40)  # Passport: bin2hex(random_bytes(40))
        now = time.time()
        payload: dict[str, Any] = {
            "aud": str(client_id),
            "jti": jti,
            "iat": now,
            "nbf": now + nbf_in,
            "exp": now + exp_in,
            "sub": str(user_id),
            "scopes": [],
        }
        payload.update(claims or {})
        token = jwt.encode(payload, key or self.private_key, algorithm="RS256", headers={"typ": "JWT"})
        if db_row:
            expires = db_expires_at or datetime.now(UTC).replace(tzinfo=None) + timedelta(days=365)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO oauth_access_tokens VALUES (?, ?, ?, ?, ?)",
                    (
                        jti,
                        db_user_id if db_user_id is not None else user_id,
                        db_client_id if db_client_id is not None else client_id,
                        int(revoked),
                        _db_time(expires),
                    ),
                )
        return token


@pytest.fixture
def make_token(rsa_private_key: rsa.RSAPrivateKey, db_path: Path) -> TokenFactory:
    return TokenFactory(rsa_private_key, db_path)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def settings_factory(public_pem: str) -> Callable[..., Settings]:
    def build(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "app_env": "testing",
            "log_level": "WARNING",
            "db_database": "laravel",
            "db_username": "agent",
            "db_password": "p@ss:w/rd#1",
            "passport_public_key": public_pem,
            "ollama_base_url": OLLAMA_URL,
            "ollama_model": MODEL,
            "ollama_warmup": False,
            "cors_origins": ["http://localhost:4200"],
            "rate_limit_per_minute": 100,
            "topic_filter_enabled": False,  # enabled explicitly in tests/test_guest_chat.py
            "data_queries_enabled": False,  # enabled explicitly in tests/test_data_queries.py
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)  # type: ignore[call-arg]

    return build


@pytest.fixture
def ollama() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=OLLAMA_URL, assert_all_called=False) as router:
        yield router


def _mysql_functions(dbapi_connection: Any, _record: Any) -> None:
    """MySQL functions the fixed money and people queries use, for the SQLite stand-in."""

    def field(value: Any, *options: Any) -> int:
        return options.index(value) + 1 if value in options else 0

    dbapi_connection.create_function("FIELD", -1, field)
    dbapi_connection.create_function("JSON_UNQUOTE", 1, lambda value: value)
    dbapi_connection.create_function(
        "CONCAT_WS", -1, lambda sep, *parts: sep.join(str(p) for p in parts if p is not None)
    )


@pytest.fixture
def client_factory(
    settings_factory: Callable[..., Settings], db_path: Path, ollama: respx.MockRouter
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def build(**overrides: Any) -> TestClient:
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        event.listen(engine.sync_engine, "connect", _mysql_functions)
        app = create_app(settings_factory(**overrides), engine=engine)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(client_factory: Callable[..., TestClient]) -> TestClient:
    return client_factory()
