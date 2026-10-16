"""Current-user endpoint, used by the frontend to confirm auth works."""

from __future__ import annotations

from fastapi import APIRouter

from app.auth.dependencies import CurrentUserDep
from app.auth.models import CurrentUser
from app.schemas.errors import ERROR_RESPONSES

router = APIRouter(prefix="/v1", tags=["auth"])


@router.get("/me", responses={401: ERROR_RESPONSES[401]})
async def me(user: CurrentUserDep) -> CurrentUser:
    """Return the user the bearer token resolves to."""
    return user
