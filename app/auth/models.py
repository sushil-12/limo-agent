"""Authenticated principal models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CurrentUser(BaseModel):
    """The Laravel user a bearer token resolves to.

    ``role`` is the service-level role name (from AUTH_ALLOWED_ROLES) and ``role_id``
    the raw ``users.role`` value. ``abilities`` are the Passport token scopes.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    role: str
    role_id: int
    name: str | None
    first_name: str | None
    email: str | None
    token_id: str
    client_id: int
    abilities: tuple[str, ...] = ()
