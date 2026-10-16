"""FastAPI dependencies for user and service authentication."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Request

from app.auth.models import CurrentUser
from app.auth.passport import PassportTokenVerifier
from app.core.errors import UnauthorizedError

SERVICE_KEY_HEADER = "X-Service-Key"


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def get_current_user(request: Request) -> CurrentUser:
    """Resolve ``Authorization: Bearer <token>`` to a Laravel user, or raise 401."""
    token = _bearer_token(request)
    if token is None:
        raise UnauthorizedError()
    verifier: PassportTokenVerifier = request.app.state.token_verifier
    user = await verifier.verify(token)
    if user is None:
        raise UnauthorizedError()
    return user


async def require_service_key(request: Request) -> None:
    """Allow only callers presenting SERVICE_API_KEY (server-to-server/admin)."""
    expected = request.app.state.settings.service_api_key
    provided = request.headers.get(SERVICE_KEY_HEADER)
    if expected is None or not provided:
        raise UnauthorizedError()
    if not hmac.compare_digest(provided.encode(), expected.get_secret_value().encode()):
        raise UnauthorizedError()


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
