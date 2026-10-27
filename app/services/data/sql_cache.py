"""Learned question -> SQL pairs, so a repeated question skips writing SQL.

When a question is answered by a query that ran, the pair is appended to a JSONL file
and kept in memory. When the same question is asked again (same role, same words
after ignoring case and punctuation) the stored SQL goes straight to ``sql_guard``
and the database, with no SQL-writing model call, and the topic guard call is skipped
too since the question is known to be a data question. The guard still scopes the
query to whoever asks, so a rider's cached query only ever returns that rider's rows.

A follow-up ("and last month?") depends on the question before it, so the key of a
later turn in a conversation includes the previous user message.

Not learned: a query with a date or year the question doesn't mention (the model
turned "today" into a fixed date that would be wrong tomorrow).

A cached query that stops working (schema change) is forgotten and the model writes
a new one. To correct a wrong answer, delete its lines from the file and restart, or
add the right pair to ``sql_examples.jsonl`` and delete the learned one.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import orjson

from app.core.logging import get_logger
from app.llm.base import LLMMessage
from app.services.data.query_log import JsonlLog

logger = get_logger(__name__)

_WORD = re.compile(r"\w+", re.UNICODE)
_DATE_LITERAL = re.compile(r"\b(?:19|20)\d{2}(?:-\d{1,2}(?:-\d{1,2})?)?\b")
_FOLLOW_UP_SEPARATOR = " || "


def normalise(text: str) -> str:
    """Lower-case words separated by single spaces: punctuation and spacing don't matter."""
    return " ".join(_WORD.findall(text.lower()))


def cache_key(history: Sequence[LLMMessage], window: int) -> str | None:
    """The key for the last user message in ``history``, given the turns the SQL model sees."""
    asked = [m.content for m in history[-window:] if m.role == "user"]
    if not asked or not (current := normalise(asked[-1])):
        return None
    if len(asked) == 1:
        return current
    return normalise(asked[-2]) + _FOLLOW_UP_SEPARATOR + current


def is_reusable(question: str, sql: str) -> bool:
    """False when the SQL pins a date or year the question didn't ask for."""
    asked = set(_DATE_LITERAL.findall(question))
    return all(found in asked for found in _DATE_LITERAL.findall(sql))


class SqlCache:
    def __init__(self, path: Path, *, max_entries: int = 5000) -> None:
        self._file = JsonlLog(path)
        self._max_entries = max_entries
        self._entries: dict[tuple[str, str], str] = {}
        self._load(path)

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, role: str, key: str | None) -> str | None:
        return self._entries.get((role, key)) if key is not None else None

    async def remember(self, role: str, key: str | None, question: str, sql: str) -> None:
        if key is None or not is_reusable(question, sql) or self._entries.get((role, key)) == sql:
            return
        if (role, key) not in self._entries and len(self._entries) >= self._max_entries:
            logger.warning("sql_cache_full", max_entries=self._max_entries)
            return
        self._entries[(role, key)] = sql
        await self._file.write({"role": role, "key": key, "question": question, "sql": sql})

    async def forget(self, role: str, key: str | None, reason: str) -> None:
        if key is None or self._entries.pop((role, key), None) is None:
            return
        logger.info("sql_cache_forget", role=role, key=key, reason=reason)
        await self._file.write({"role": role, "key": key, "sql": None, "reason": reason})

    def _load(self, path: Path) -> None:
        """Replay the file: later lines win, ``"sql": null`` removes a pair."""
        if not path.exists():
            return
        for number, line in enumerate(path.read_bytes().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = orjson.loads(line)
                slot = (str(item["role"]), str(item["key"]))
                sql = item["sql"]
            except (orjson.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning("sql_cache_line_skipped", line=number, error=str(exc))
                continue
            if sql:
                self._entries[slot] = str(sql)
            else:
                self._entries.pop(slot, None)
        logger.info("sql_cache_loaded", path=str(path), entries=len(self._entries))
