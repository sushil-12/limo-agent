"""Append-only JSONL log of every data question and the SQL written for it.

One line per question: what was asked, by which role, which tables were offered, each
SQL attempt with the reason it was rejected, and the outcome. Result rows are never
written (they may hold personal data). Use it to review queries and to collect
corrected question/SQL pairs for ``sql_examples.jsonl``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

import orjson

from app.core.logging import get_logger

logger = get_logger(__name__)


class QueryLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    async def write(self, record: dict[str, Any]) -> None:
        """Append one record. Never raises: logging must not break a reply."""
        line = orjson.dumps({"ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), **record}) + b"\n"
        try:
            async with self._lock:
                await asyncio.to_thread(self._append, line)
        except OSError as exc:
            logger.warning("query_log_write_failed", path=str(self._path), error=str(exc))

    def _append(self, line: bytes) -> None:
        with self._path.open("ab") as handle:
            handle.write(line)
