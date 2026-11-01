"""FCM device tokens per user (``tbl_rt_user_devices``)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.realtime.db import utcnow
from app.realtime.tables import user_devices


class DeviceRegistry:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def register(
        self,
        user_id: int,
        *,
        token: str,
        app: str,
        platform: str,
        app_version: str | None,
        locale: str | None,
    ) -> None:
        """Upsert by token. A token that moves to another user (re-login) moves with it."""
        now = utcnow()
        values = {
            "user_id": user_id,
            "app": app,
            "platform": platform,
            "app_version": app_version,
            "locale": locale,
            "last_seen_at": now,
            "revoked_at": None,
        }
        for _ in range(2):
            async with self._engine.begin() as conn:
                existing = await conn.scalar(
                    select(user_devices.c.id).where(user_devices.c.fcm_token == token)
                )
                if existing is not None:
                    await conn.execute(
                        user_devices.update().where(user_devices.c.id == existing).values(**values)
                    )
                    return
                try:
                    await conn.execute(
                        user_devices.insert().values(fcm_token=token, created_at=now, **values)
                    )
                except IntegrityError:
                    continue  # registered concurrently; update it on the second pass
                return

    async def revoke_token(self, token: str, *, user_id: int | None = None) -> None:
        query = user_devices.update().where(user_devices.c.fcm_token == token)
        if user_id is not None:
            query = query.where(user_devices.c.user_id == user_id)
        async with self._engine.begin() as conn:
            await conn.execute(query.values(revoked_at=utcnow()))

    async def active_tokens(self, user_id: int) -> list[str]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(user_devices.c.fcm_token)
                .where(user_devices.c.user_id == user_id, user_devices.c.revoked_at.is_(None))
                .order_by(user_devices.c.last_seen_at.desc())
                .limit(10)
            )
            return [r[0] for r in rows]
