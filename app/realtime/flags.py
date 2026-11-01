"""Kill switches and rollout flags (``tbl_rt_settings``), editable from admin.

Read into memory and refreshed every RT_FLAGS_REFRESH_SECONDS, so a change takes effect
within half a minute without a restart or an app release. Laravel's ``mobile/v1/config``
reads the same table to tell the apps which realtime provider to use.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.logging import get_logger
from app.realtime.db import utcnow
from app.realtime.tables import settings as settings_table

logger = get_logger(__name__)

DEFAULT_FLAGS: dict[str, Any] = {
    "rt.provider": "node",  # node | python: which realtime service the apps connect to
    "rt.url": "",  # wss URL of limo-rt, sent to apps when the provider is python
    "rt.allowlist_user_ids": [],  # pilot users who get python while rt.provider is node
    "rt.proximity_alerts": True,
    "rt.sms_fallback": True,
    "app.min_version": {"ios_driver": "", "ios_rider": "", "android_driver": "", "android_rider": ""},
}


def _valid(name: str, value: Any) -> bool:
    if name not in DEFAULT_FLAGS:
        return False
    default = DEFAULT_FLAGS[name]
    if name == "rt.provider":
        return value in ("node", "python")
    if name == "rt.allowlist_user_ids":
        return isinstance(value, list) and all(isinstance(v, int) and v > 0 for v in value)
    if name == "app.min_version":
        return (
            isinstance(value, dict)
            and set(value) <= set(default)
            and all(isinstance(v, str) for v in value.values())
        )
    return isinstance(value, type(default))


class Flags:
    def __init__(self, engine: AsyncEngine, *, refresh_seconds: float = 30.0) -> None:
        self._engine = engine
        self._refresh = refresh_seconds
        self._values: dict[str, Any] = dict(DEFAULT_FLAGS)
        self._task: asyncio.Task[None] | None = None

    def get(self, name: str) -> Any:
        return self._values.get(name, DEFAULT_FLAGS.get(name))

    def all(self) -> dict[str, Any]:
        return dict(self._values)

    def enabled(self, name: str) -> bool:
        return bool(self.get(name))

    async def load(self) -> None:
        try:
            async with self._engine.connect() as conn:
                rows = (await conn.execute(select(settings_table.c.name, settings_table.c.value))).all()
        except SQLAlchemyError as exc:
            logger.warning("rt_flags_load_failed", error=type(exc).__name__)
            return
        values = dict(DEFAULT_FLAGS)
        for name, value in rows:
            if _valid(name, value):
                values[name] = value
            else:
                logger.warning("rt_flag_ignored", name=name)
        self._values = values

    async def set(self, name: str, value: Any, *, user_id: int | None) -> None:
        if not _valid(name, value):
            raise ValueError(f"invalid value for flag {name!r}")
        now = utcnow()
        async with self._engine.begin() as conn:
            exists = await conn.scalar(select(settings_table.c.name).where(settings_table.c.name == name))
            if exists is None:
                await conn.execute(
                    settings_table.insert().values(
                        name=name, value=value, updated_by_user_id=user_id, updated_at=now
                    )
                )
            else:
                await conn.execute(
                    settings_table.update()
                    .where(settings_table.c.name == name)
                    .values(value=value, updated_by_user_id=user_id, updated_at=now)
                )
        self._values[name] = value
        logger.info("rt_flag_changed", name=name, user_id=user_id)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="rt-flags")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh)
            await self.load()
