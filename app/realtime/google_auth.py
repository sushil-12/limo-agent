"""OAuth access tokens for a Google service account (no Google SDK).

Signs a JWT with the account's private key, exchanges it at Google's token endpoint for a
one-hour access token, and caches it until a minute before expiry. Used for FCM and
Document AI.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import jwt

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret
SCOPE_FCM = "https://www.googleapis.com/auth/firebase.messaging"
SCOPE_CLOUD = "https://www.googleapis.com/auth/cloud-platform"


class ServiceAccountTokens:
    def __init__(self, client: httpx.AsyncClient, credentials: dict[str, str], scope: str) -> None:
        self._client = client
        self._creds = credentials
        self._scope = scope
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Forget the cached token (the API answered 401)."""
        self._token = None

    async def get(self) -> str:
        async with self._lock:
            if self._token is not None and time.time() < self._expires - 60:
                return self._token
            now = int(time.time())
            assertion = jwt.encode(
                {
                    "iss": self._creds["client_email"],
                    "scope": self._scope,
                    "aud": TOKEN_URL,
                    "iat": now,
                    "exp": now + 3600,
                },
                self._creds["private_key"],
                algorithm="RS256",
            )
            response = await self._client.post(
                TOKEN_URL,
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
                timeout=10.0,
            )
            response.raise_for_status()
            payload = response.json()
            self._token = str(payload["access_token"])
            self._expires = now + float(payload.get("expires_in", 3600))
            return self._token
