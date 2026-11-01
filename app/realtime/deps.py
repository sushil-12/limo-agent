"""Request dependencies for the realtime REST API."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Request

from app.auth.dependencies import bearer_token
from app.auth.models import CurrentUser
from app.core.errors import AppError, UnauthorizedError
from app.realtime.services import Realtime
from app.realtime.share import ShareGrant, is_share_token

INTERNAL_SECRET_HEADER = "x-secret"  # noqa: S105 - a header name


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"
    message = "You don't have access to this ride."


class UnprocessableError(AppError):
    status_code = 422
    code = "validation_error"


def get_rt(request: Request) -> Realtime:
    return request.app.state.rt  # type: ignore[no-any-return]


RtDep = Annotated[Realtime, Depends(get_rt)]


async def get_user(request: Request) -> CurrentUser:
    token = bearer_token(request)
    if token is None or is_share_token(token):
        raise UnauthorizedError()
    user = await get_rt(request).verifier.verify(token)
    if user is None:
        raise UnauthorizedError()
    return user


UserDep = Annotated[CurrentUser, Depends(get_user)]


async def get_user_or_share(request: Request) -> CurrentUser | ShareGrant:
    """A Passport user, or a share-link grant (read-only, one ride)."""
    token = bearer_token(request)
    if token is None:
        raise UnauthorizedError()
    rt = get_rt(request)
    if is_share_token(token):
        grant = rt.shares.verify(token)
        if grant is None:
            raise UnauthorizedError()
        return grant
    user = await rt.verifier.verify(token)
    if user is None:
        raise UnauthorizedError()
    return user


ViewerDep = Annotated[CurrentUser | ShareGrant, Depends(get_user_or_share)]


async def require_admin(user: UserDep) -> CurrentUser:
    if user.role != "admin":
        raise ForbiddenError("Admin only.")
    return user


AdminDep = Annotated[CurrentUser, Depends(require_admin)]


async def require_internal(request: Request) -> None:
    """Laravel -> limo-rt. Also restrict these paths to the private network at the proxy."""
    expected = get_rt(request).settings.rt_internal_secret.get_secret_value()
    provided = request.headers.get(INTERNAL_SECRET_HEADER) or ""
    if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise UnauthorizedError()


InternalDep = Depends(require_internal)
