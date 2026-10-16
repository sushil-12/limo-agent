"""Passport bearer-token verification via GET /v1/me."""

from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import orjson
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from tests.conftest import (
    ADMIN_ID,
    CLIENT_DRIVERS,
    CLIENT_REVOKED,
    DELETED_ID,
    DISABLED_ID,
    DRIVER_ID,
    RIDER_ID,
    TokenFactory,
    auth,
)

UNAUTHORIZED_MESSAGE = "Invalid or missing credentials."


def assert_unauthorized(response: httpx.Response) -> None:
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    error = response.json()["error"]
    assert error["code"] == "unauthorized"
    assert error["message"] == UNAUTHORIZED_MESSAGE  # never reveals which check failed
    assert error["request_id"] == response.headers["x-request-id"]


def test_valid_rider_token(client: TestClient, make_token: TokenFactory) -> None:
    response = client.get("/v1/me", headers=auth(make_token(RIDER_ID)))
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == RIDER_ID
    assert body["role"] == "rider"
    assert body["role_id"] == 5
    assert body["first_name"] == "Asha"
    assert body["email"] == "asha@example.com"
    assert body["client_id"] == 1
    assert len(body["token_id"]) == 80


def test_valid_driver_token(client: TestClient, make_token: TokenFactory) -> None:
    response = client.get("/v1/me", headers=auth(make_token(DRIVER_ID)))
    assert response.status_code == 200
    assert response.json()["role"] == "driver"


def test_aud_as_single_item_list_accepted(client: TestClient, make_token: TokenFactory) -> None:
    token = make_token(RIDER_ID, claims={"aud": ["1"]})
    assert client.get("/v1/me", headers=auth(token)).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer   "},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": "Bearer a.b.c"},
        {"Authorization": "Bearer 12|abcdefghijklmnopqrstuvwxyz"},  # Sanctum-style token
    ],
)
def test_missing_or_malformed_header(client: TestClient, headers: dict[str, str]) -> None:
    assert_unauthorized(client.get("/v1/me", headers=headers))


def test_token_signed_with_other_key(client: TestClient, make_token: TokenFactory) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert_unauthorized(client.get("/v1/me", headers=auth(make_token(RIDER_ID, key=other))))


def test_tampered_payload(client: TestClient, make_token: TokenFactory) -> None:
    header, payload, signature = make_token(RIDER_ID).split(".")
    claims = orjson.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["sub"] = str(DRIVER_ID)
    forged = base64.urlsafe_b64encode(orjson.dumps(claims)).rstrip(b"=").decode()
    assert_unauthorized(client.get("/v1/me", headers=auth(f"{header}.{forged}.{signature}")))


def test_alg_none_rejected(client: TestClient, make_token: TokenFactory) -> None:
    good = make_token(RIDER_ID)
    claims = jwt.decode(good, options={"verify_signature": False})
    unsigned = jwt.encode(claims, key=None, algorithm="none")
    assert_unauthorized(client.get("/v1/me", headers=auth(unsigned)))


def test_hs256_with_public_key_rejected(
    client: TestClient, public_pem: str, make_token: TokenFactory
) -> None:
    """Algorithm-confusion attack: HMAC-sign with the (public) RSA key."""
    claims = jwt.decode(make_token(RIDER_ID), options={"verify_signature": False})
    header = base64.urlsafe_b64encode(b'{"typ":"JWT","alg":"HS256"}').rstrip(b"=")
    body = base64.urlsafe_b64encode(orjson.dumps(claims)).rstrip(b"=")
    sig = base64.urlsafe_b64encode(
        hmac.new(public_pem.encode(), header + b"." + body, hashlib.sha256).digest()
    ).rstrip(b"=")
    token = b".".join([header, body, sig]).decode()
    assert_unauthorized(client.get("/v1/me", headers=auth(token)))


def test_expired_jwt(client: TestClient, make_token: TokenFactory) -> None:
    assert_unauthorized(client.get("/v1/me", headers=auth(make_token(RIDER_ID, exp_in=-60))))


def test_not_yet_valid_jwt(client: TestClient, make_token: TokenFactory) -> None:
    assert_unauthorized(client.get("/v1/me", headers=auth(make_token(RIDER_ID, nbf_in=600))))


def test_expired_via_db_expires_at(client: TestClient, make_token: TokenFactory) -> None:
    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
    token = make_token(RIDER_ID, db_expires_at=past)
    assert_unauthorized(client.get("/v1/me", headers=auth(token)))


def test_revoked_token(client: TestClient, make_token: TokenFactory) -> None:
    assert_unauthorized(client.get("/v1/me", headers=auth(make_token(RIDER_ID, revoked=True))))


def test_unknown_token_id(client: TestClient, make_token: TokenFactory) -> None:
    assert_unauthorized(client.get("/v1/me", headers=auth(make_token(RIDER_ID, db_row=False))))


def test_revoked_client(client: TestClient, make_token: TokenFactory) -> None:
    token = make_token(RIDER_ID, client_id=CLIENT_REVOKED)
    assert_unauthorized(client.get("/v1/me", headers=auth(token)))


def test_client_for_other_provider(client: TestClient, make_token: TokenFactory) -> None:
    """Tokens issued for TblDriverSetting (provider 'drivers') must not map to users."""
    token = make_token(RIDER_ID, client_id=CLIENT_DRIVERS)
    assert_unauthorized(client.get("/v1/me", headers=auth(token)))


def test_sub_does_not_match_token_row(client: TestClient, make_token: TokenFactory) -> None:
    token = make_token(DRIVER_ID, db_user_id=RIDER_ID)
    assert_unauthorized(client.get("/v1/me", headers=auth(token)))


def test_aud_does_not_match_token_row(client: TestClient, make_token: TokenFactory) -> None:
    token = make_token(RIDER_ID, client_id=1, db_client_id=CLIENT_DRIVERS)
    assert_unauthorized(client.get("/v1/me", headers=auth(token)))


def test_role_not_allowed(client: TestClient, make_token: TokenFactory) -> None:
    assert_unauthorized(client.get("/v1/me", headers=auth(make_token(ADMIN_ID))))


def test_role_allowed_by_config(client_factory, make_token: TokenFactory) -> None:
    client = client_factory(auth_allowed_roles={1: "admin"})
    response = client.get("/v1/me", headers=auth(make_token(ADMIN_ID)))
    assert response.status_code == 200
    assert response.json()["role"] == "admin"


def test_soft_deleted_user(client: TestClient, make_token: TokenFactory) -> None:
    assert_unauthorized(client.get("/v1/me", headers=auth(make_token(DELETED_ID))))


def test_disabled_account(client: TestClient, make_token: TokenFactory) -> None:
    assert_unauthorized(client.get("/v1/me", headers=auth(make_token(DISABLED_ID))))


def test_user_row_missing(client: TestClient, make_token: TokenFactory) -> None:
    assert_unauthorized(client.get("/v1/me", headers=auth(make_token(999))))


def test_success_is_cached_by_token_hash(client: TestClient, make_token: TokenFactory, db_path: Path) -> None:
    token = make_token(RIDER_ID)
    assert client.get("/v1/me", headers=auth(token)).status_code == 200

    verifier = client.app.state.token_verifier
    assert list(verifier._cache) == [hashlib.sha256(token.encode()).hexdigest()]
    assert all(token not in key for key in verifier._cache)

    with sqlite3.connect(db_path) as conn:  # revoke after the first call
        conn.execute("UPDATE oauth_access_tokens SET revoked = 1")
    assert client.get("/v1/me", headers=auth(token)).status_code == 200  # cached (<= TTL)


def test_revocation_immediate_without_cache(client_factory, make_token: TokenFactory, db_path: Path) -> None:
    client = client_factory(auth_cache_ttl_seconds=0)
    token = make_token(RIDER_ID)
    assert client.get("/v1/me", headers=auth(token)).status_code == 200
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE oauth_access_tokens SET revoked = 1")
    assert_unauthorized(client.get("/v1/me", headers=auth(token)))


def test_failures_are_not_cached(client: TestClient, make_token: TokenFactory) -> None:
    client.get("/v1/me", headers=auth(make_token(RIDER_ID, revoked=True)))
    assert client.app.state.token_verifier._cache == {}
