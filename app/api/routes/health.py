"""Liveness and readiness probes (no auth)."""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.api.deps import HealthServiceDep
from app.schemas.health import LivenessResponse, ReadinessResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> LivenessResponse:
    """Process is up. No dependency checks."""
    return LivenessResponse()


@router.get("/ready", responses={503: {"model": ReadinessResponse}})
async def ready(response: Response, health: HealthServiceDep) -> ReadinessResponse:
    """DB and LLM checked concurrently; 503 if any dependency is down."""
    result = await health.readiness()
    if result.status != "ok":
        response.status_code = 503
    return result
