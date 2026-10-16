"""Laravel Passport (league/oauth2-server 8.x) access-token verification.

Mirrors ``Laravel\\Passport\\Guards\\TokenGuard::authenticateViaBearerToken``:

1. The bearer token is an RS256 JWT signed with Laravel's ``oauth-private.key``.
   Verify the signature with the matching public key, plus ``exp``/``nbf``/``iat``.
   Claims: ``jti`` = oauth_access_tokens.id, ``aud`` = client id, ``sub`` = user id.
2. The token row must exist and not be revoked (``isAccessTokenRevoked``). We also
   honour ``expires_at`` as defence in depth.
3. The client must exist, not be revoked, and its ``provider`` must be empty or
   ``users`` (the provider of the ``api``/``mobile`` guards).
4. The user must exist. Additionally (service policy): ``users.soft_delete`` must be
   0, ``tbl_account.status`` must not be ``disable`` (like Laravel's ``userStatus``
   middleware), and ``users.role`` must be in AUTH_ALLOWED_ROLES.

All checks after signature verification run in ONE parameterised query. Successful
results are cached in memory, keyed by sha256(token), for AUTH_CACHE_TTL_SECONDS, so
a revoked token keeps working for at most that long on this worker.

TODO: Passport does not track last-used time, and this service has read-only DB
access. If usage tracking is needed, add it through Laravel or a writable account.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from sqlalchemy import Boolean, DateTime, Integer, String, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth.models import CurrentUser
from app.core.errors import ServiceUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

MAX_TOKEN_LENGTH = 8192
USERS_PROVIDER = "users"

_TOKEN_QUERY = text(
    """
    SELECT t.user_id, t.client_id, t.revoked AS token_revoked, t.expires_at,
           c.revoked AS client_revoked, c.provider,
           u.role, u.name, u.email, u.soft_delete AS user_soft_delete,
           a.first_name, a.status AS account_status, a.soft_delete AS account_soft_delete
    FROM oauth_access_tokens t
    JOIN oauth_clients c ON c.id = t.client_id
    JOIN users u ON u.id = t.user_id
    LEFT JOIN tbl_account a ON a.user_id = u.id
    WHERE t.id = :jti
    ORDER BY a.id
    LIMIT 1
    """
).columns(
    user_id=Integer(),
    client_id=Integer(),
    token_revoked=Boolean(),
    expires_at=DateTime(),
    client_revoked=Boolean(),
    provider=String(),
    role=Integer(),
    name=String(),
    email=String(),
    user_soft_delete=Integer(),
    first_name=String(),
    account_status=String(),
    account_soft_delete=Integer(),
)


class TokenRejectedError(Exception):
    """Internal: why a token was rejected. Logged, never returned to the client."""


@dataclass(slots=True)
class _CacheEntry:
    user: CurrentUser
    expires_at: float  # time.monotonic()


@dataclass(frozen=True, slots=True)
class _Claims:
    token_id: str
    user_id: int
    client_id: int
    expires_at: float  # unix seconds
    scopes: tuple[str, ...]


class PassportTokenVerifier:
    """Verifies Passport bearer tokens against the public key and the Laravel DB."""

    def __init__(
        self,
        engine: AsyncEngine,
        public_key_pem: str,
        *,
        allowed_roles: dict[int, str],
        leeway_seconds: int = 5,
        cache_ttl_seconds: float = 60.0,
        cache_max_entries: int = 10_000,
    ) -> None:
        key = load_pem_public_key(public_key_pem.encode())
        if not isinstance(key, RSAPublicKey):
            raise ValueError("Passport public key must be an RSA key")
        self._engine = engine
        self._key = key
        self._allowed_roles = dict(allowed_roles)
        self._leeway = leeway_seconds
        self._cache_ttl = cache_ttl_seconds
        self._cache_max = cache_max_entries
        self._cache: dict[str, _CacheEntry] = {}

    async def verify(self, token: str) -> CurrentUser | None:
        """Return the user for a valid token, or ``None`` if it must be rejected.

        Raises ServiceUnavailableError if the database cannot be reached.
        """
        cache_key = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cache.get(cache_key)
        if cached is not None:
            if cached.expires_at > time.monotonic():
                return cached.user
            del self._cache[cache_key]

        try:
            claims = self._decode(token)
            user = await self._load_user(claims)
        except TokenRejectedError as exc:
            logger.info("auth_rejected", reason=str(exc))
            return None

        ttl = min(self._cache_ttl, claims.expires_at - time.time())
        if ttl > 0:
            self._store(cache_key, _CacheEntry(user, time.monotonic() + ttl))
        return user

    def _decode(self, token: str) -> _Claims:
        if len(token) > MAX_TOKEN_LENGTH or token.count(".") != 2:
            raise TokenRejectedError("malformed")
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                self._key,
                algorithms=["RS256"],
                leeway=self._leeway,
                options={"require": ["exp", "jti", "sub", "aud"], "verify_aud": False},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenRejectedError("jwt_expired") from exc
        except jwt.PyJWTError as exc:
            raise TokenRejectedError(f"jwt_invalid:{type(exc).__name__}") from exc

        aud = payload["aud"]
        if isinstance(aud, list):
            aud = aud[0] if len(aud) == 1 else None
        sub, jti = payload["sub"], payload["jti"]
        if not (isinstance(aud, str | int) and str(aud).isdigit()):
            raise TokenRejectedError("bad_aud")
        if not (isinstance(sub, str | int) and str(sub).isdigit()):
            raise TokenRejectedError("bad_sub")  # e.g. client_credentials tokens
        if not (isinstance(jti, str) and 0 < len(jti) <= 100):
            raise TokenRejectedError("bad_jti")
        scopes = payload.get("scopes") or []
        return _Claims(
            token_id=jti,
            user_id=int(sub),
            client_id=int(aud),
            expires_at=float(payload["exp"]),
            scopes=tuple(str(s) for s in scopes) if isinstance(scopes, list) else (),
        )

    async def _load_user(self, claims: _Claims) -> CurrentUser:
        try:
            async with self._engine.connect() as conn:
                row = (await conn.execute(_TOKEN_QUERY, {"jti": claims.token_id})).first()
        except (SQLAlchemyError, OSError) as exc:
            logger.error("auth_db_error", error_type=type(exc).__name__)
            raise ServiceUnavailableError() from exc

        if row is None:
            raise TokenRejectedError("token_or_client_or_user_not_found")
        r = row._mapping
        # Laravel stores timestamps in the app timezone (UTC); values are naive.
        now_utc = datetime.now(UTC).replace(tzinfo=None)
        if r["token_revoked"]:
            raise TokenRejectedError("token_revoked")
        if r["expires_at"] is not None and r["expires_at"] <= now_utc:
            raise TokenRejectedError("token_expired")
        if r["client_revoked"]:
            raise TokenRejectedError("client_revoked")
        if r["provider"] not in (None, "", USERS_PROVIDER):
            raise TokenRejectedError("client_provider_mismatch")
        if r["user_id"] != claims.user_id or r["client_id"] != claims.client_id:
            raise TokenRejectedError("claims_mismatch")
        if r["user_soft_delete"]:
            raise TokenRejectedError("user_deleted")
        if r["account_status"] == "disable" or r["account_soft_delete"]:
            raise TokenRejectedError("account_disabled")
        role_name = self._allowed_roles.get(r["role"])
        if role_name is None:
            raise TokenRejectedError("role_not_allowed")

        return CurrentUser(
            id=claims.user_id,
            role=role_name,
            role_id=r["role"],
            name=r["name"],
            first_name=r["first_name"],
            email=r["email"],
            token_id=claims.token_id,
            client_id=claims.client_id,
            abilities=claims.scopes,
        )

    def _store(self, key: str, entry: _CacheEntry) -> None:
        if len(self._cache) >= self._cache_max:
            now = time.monotonic()
            for stale in [k for k, v in self._cache.items() if v.expires_at <= now]:
                del self._cache[stale]
            while len(self._cache) >= self._cache_max:
                del self._cache[next(iter(self._cache))]  # oldest insertion
        self._cache[key] = entry
