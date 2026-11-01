"""Fire-and-forget background tasks that are tracked, logged and cancelled on shutdown."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Coroutine
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


class TaskSet:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("rt_background_task_failed", task=task.get_name(), error=repr(exc), exc_info=exc)

    async def drain(self, grace_seconds: float = 5.0) -> None:
        """Let in-flight work finish briefly, then cancel the rest.

        Tasks may spawn follow-ups (a push waiting for an ack), so keep waiting until the
        set is empty or the grace period is over.
        """
        deadline = asyncio.get_running_loop().time() + grace_seconds
        while self._tasks:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.wait(set(self._tasks), timeout=remaining)
        pending = set(self._tasks)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def __len__(self) -> int:
        return len(self._tasks)
