"""Signed share links for the public web tracking page.

A share token is an HS256 JWT ``{typ:"share", rid, jti, iat, exp}`` signed with
RT_SHARE_TOKEN_SECRET. ``exp`` is a hard cap (RT_SHARE_TOKEN_MAX_HOURS); the token also
stops working as soon as the ride is over, which is checked on every use against the
ride's current status, so a link never outlives its ride.

It grants read-only access to one ride: location, status and the driver's first name.
No chat, no phone numbers.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

import jwt

SHARE_TYP = "share"
_ALG = "HS256"


@dataclass(frozen=True, slots=True)
class ShareGrant:
    ride_id: int
    token_id: str
    expires_at: int


class ShareTokens:
    def __init__(self, secret: str, *, max_hours: float) -> None:
        self._secret = secret
        self._max_seconds = int(max_hours * 3600)

    def mint(self, ride_id: int) -> tuple[str, int]:
        now = int(time.time())
        exp = now + self._max_seconds
        token = jwt.encode(
            {"typ": SHARE_TYP, "rid": ride_id, "jti": secrets.token_urlsafe(12), "iat": now, "exp": exp},
            self._secret,
            algorithm=_ALG,
        )
        return token, exp

    def verify(self, token: str) -> ShareGrant | None:
        try:
            claims = jwt.decode(
                token, self._secret, algorithms=[_ALG], options={"require": ["exp", "rid", "jti", "typ"]}
            )
        except jwt.PyJWTError:
            return None
        if claims.get("typ") != SHARE_TYP or not isinstance(claims.get("rid"), int):
            return None
        return ShareGrant(ride_id=claims["rid"], token_id=str(claims["jti"]), expires_at=int(claims["exp"]))


def is_share_token(token: str) -> bool:
    """Passport tokens are RS256; share tokens are HS256. Peek at the header only."""
    try:
        return jwt.get_unverified_header(token).get("alg") == _ALG
    except jwt.PyJWTError:
        return False
