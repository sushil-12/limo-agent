"""Ride chat (``tbl_rt_chat_messages``), replacing limoRTService's Mongo ``messages``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.realtime.db import utcnow
from app.realtime.tables import chat_messages

MAX_BODY = 2000


@dataclass(frozen=True, slots=True)
class ChatMessage:
    id: int
    ride_id: int
    sender_user_id: int
    receiver_user_id: int | None
    sender_role: str
    body: str
    client_msg_id: str | None
    created_at: datetime
    read_at: datetime | None

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "from": self.sender_role,
            "sender_id": str(self.sender_user_id),
            "receiver_id": str(self.receiver_user_id) if self.receiver_user_id else None,
            "body": self.body,
            "client_msg_id": self.client_msg_id,
            "at": utc_ms(self.created_at),
            "read_at": utc_ms(self.read_at) if self.read_at else None,
        }


def utc_ms(value: datetime) -> int:
    """Naive-UTC datetime (as stored) -> epoch ms."""
    return int(value.replace(tzinfo=UTC).timestamp() * 1000)


def _from_row(row: Any) -> ChatMessage:
    m = row._mapping
    created = m["created_at"]
    if isinstance(created, str):  # SQLite in tests
        created = datetime.fromisoformat(created)
    read = m["read_at"]
    if isinstance(read, str):
        read = datetime.fromisoformat(read)
    return ChatMessage(
        id=int(m["id"]),
        ride_id=int(m["reservation_id"]),
        sender_user_id=int(m["sender_user_id"]),
        receiver_user_id=int(m["receiver_user_id"]) if m["receiver_user_id"] else None,
        sender_role=m["sender_role"],
        body=m["body"],
        client_msg_id=m["client_msg_id"],
        created_at=created,
        read_at=read,
    )


class ChatStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(
        self,
        ride_id: int,
        *,
        sender_user_id: int,
        receiver_user_id: int | None,
        sender_role: str,
        body: str,
        client_msg_id: str | None,
    ) -> tuple[ChatMessage, bool]:
        """Insert; a retried send with the same client_msg_id returns the original (created=False)."""
        if client_msg_id:
            existing = await self._by_client_id(sender_user_id, client_msg_id)
            if existing is not None:
                return existing, False
        try:
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    chat_messages.insert().values(
                        reservation_id=ride_id,
                        sender_user_id=sender_user_id,
                        receiver_user_id=receiver_user_id,
                        sender_role=sender_role,
                        body=body[:MAX_BODY],
                        client_msg_id=client_msg_id,
                        created_at=utcnow(),
                    )
                )
                new_id = int(result.inserted_primary_key[0])
        except IntegrityError:
            if client_msg_id:
                existing = await self._by_client_id(sender_user_id, client_msg_id)
                if existing is not None:
                    return existing, False
            raise
        message = await self.get(new_id)
        assert message is not None  # noqa: S101 - just inserted
        return message, True

    async def get(self, message_id: int) -> ChatMessage | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(chat_messages).where(chat_messages.c.id == message_id))).first()
        return _from_row(row) if row is not None else None

    async def _by_client_id(self, sender_user_id: int, client_msg_id: str) -> ChatMessage | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(chat_messages).where(
                        chat_messages.c.sender_user_id == sender_user_id,
                        chat_messages.c.client_msg_id == client_msg_id,
                    )
                )
            ).first()
        return _from_row(row) if row is not None else None

    async def history(self, ride_id: int, *, after_id: int = 0, limit: int = 50) -> list[ChatMessage]:
        """Oldest first. ``after_id`` lets a reconnecting client fetch exactly what it missed."""
        query = select(chat_messages).where(chat_messages.c.reservation_id == ride_id)
        if after_id > 0:
            query = query.where(chat_messages.c.id > after_id).order_by(chat_messages.c.id).limit(limit)
            async with self._engine.connect() as conn:
                return [_from_row(r) for r in await conn.execute(query)]
        query = query.order_by(chat_messages.c.id.desc()).limit(limit)
        async with self._engine.connect() as conn:
            rows = [_from_row(r) for r in await conn.execute(query)]
        return list(reversed(rows))

    async def mark_read(self, ride_id: int, reader_user_id: int, up_to_id: int) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                chat_messages.update()
                .where(
                    chat_messages.c.reservation_id == ride_id,
                    chat_messages.c.receiver_user_id == reader_user_id,
                    chat_messages.c.id <= up_to_id,
                    chat_messages.c.read_at.is_(None),
                )
                .values(read_at=utcnow())
            )
            return int(result.rowcount or 0)
