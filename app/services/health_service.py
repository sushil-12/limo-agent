"""Readiness checks for the database and the LLM provider."""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.llm.base import LLMProvider
from app.schemas.health import DependencyCheck, LLMCheck, ReadinessChecks, ReadinessResponse


class HealthService:
    """Runs dependency checks concurrently with short timeouts."""

    def __init__(
        self,
        engine: AsyncEngine,
        provider: LLMProvider,
        *,
        timeout_seconds: float,
        expose_details: bool,
    ) -> None:
        self._engine = engine
        self._provider = provider
        self._timeout = timeout_seconds
        self._expose = expose_details

    async def readiness(self) -> ReadinessResponse:
        """Check DB and LLM together; overall status is "down" if either fails."""
        database, llm = await asyncio.gather(self._check_database(), self._check_llm())
        status = "ok" if database.status == "ok" and llm.status == "ok" else "down"
        return ReadinessResponse(status=status, checks=ReadinessChecks(database=database, llm=llm))

    async def _check_database(self) -> DependencyCheck:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
        except Exception as exc:  # any failure means "not ready"
            return DependencyCheck(
                status="down", latency_ms=_ms_since(started), detail=self._detail(type(exc).__name__)
            )
        return DependencyCheck(status="ok", latency_ms=_ms_since(started))

    async def _check_llm(self) -> LLMCheck:
        health = await self._provider.health(self._timeout)
        if not health.ok:
            detail = health.error
        elif not health.model_available:
            detail = f"model {health.model} is not pulled"
        else:
            detail = None
        return LLMCheck(
            status="ok" if health.ok and health.model_available else "down",
            latency_ms=health.latency_ms,
            model_available=health.model_available,
            detail=self._detail(detail),
        )

    def _detail(self, detail: str | None) -> str | None:
        return detail if self._expose else None


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
