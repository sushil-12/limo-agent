"""Rate limiting behind a small interface so a Redis backend can replace it later.

The in-memory implementation is per process: with N Uvicorn workers a user can make
up to N x the limit. Swap in a Redis implementation before scaling out.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of one rate-limit check."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class RateLimiter(Protocol):
    """Counts hits per key inside a rolling window."""

    async def hit(self, key: str) -> RateLimitResult:
        """Record one hit for ``key`` and say whether it is allowed."""
        ...


class InMemoryRateLimiter:
    """Sliding-window log limiter. Event-loop safe (no awaits while mutating)."""

    def __init__(self, limit: int, window_seconds: float = 60.0, max_keys: int = 100_000) -> None:
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    async def hit(self, key: str) -> RateLimitResult:
        """Record a hit; rejected hits are not counted against the window."""
        now = time.monotonic()
        cutoff = now - self.window
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= self.max_keys:
                self._evict_idle(cutoff)
            hits = self._hits[key] = deque()
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self.limit:
            retry_after = math.ceil(hits[0] + self.window - now)
            return RateLimitResult(False, self.limit, 0, retry_after)

        hits.append(now)
        return RateLimitResult(True, self.limit, self.limit - len(hits), 0)

    def _evict_idle(self, cutoff: float) -> None:
        idle = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in idle:
            del self._hits[key]
