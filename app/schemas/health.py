"""Health endpoint schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Status = Literal["ok", "down"]


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class DependencyCheck(BaseModel):
    """Status of one dependency. ``detail`` is omitted in production."""

    status: Status
    latency_ms: float
    detail: str | None = None


class LLMCheck(DependencyCheck):
    model_available: bool


class ReadinessChecks(BaseModel):
    database: DependencyCheck
    llm: LLMCheck


class ReadinessResponse(BaseModel):
    status: Status
    checks: ReadinessChecks
