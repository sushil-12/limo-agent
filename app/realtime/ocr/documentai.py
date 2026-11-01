"""Google Document AI over REST (``processors/{id}:process``), without the Google SDK.

Same behaviour as limoRTService's ``documentai.service.ts``: config check, timeout, and
logs that say which fields were found but never their values (licences are PII).
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from app.core.logging import get_logger
from app.realtime.google_auth import SCOPE_CLOUD, ServiceAccountTokens
from app.realtime.ocr.normalizer import normalize_document

logger = get_logger(__name__)


class OcrError(StrEnum):
    INVALID_CONFIG = "INVALID_CONFIG"  # -> 503
    API_ERROR = "API_ERROR"  # -> 502
    TIMEOUT = "TIMEOUT"  # -> 504
    PARSE_ERROR = "PARSE_ERROR"  # -> 502


HTTP_STATUS = {
    OcrError.INVALID_CONFIG: 503,
    OcrError.API_ERROR: 502,
    OcrError.TIMEOUT: 504,
    OcrError.PARSE_ERROR: 502,
}


@dataclass(frozen=True, slots=True)
class OcrResult:
    data: dict[str, Any] | None = None
    error: OcrError | None = None
    message: str | None = None


class DocumentAiClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        project_id: str | None,
        location: str,
        processor_id: str | None,
        credentials: dict[str, str] | None,
        timeout_seconds: float,
    ) -> None:
        self._client = client
        self._project = project_id
        self._location = location
        self._processor = processor_id
        self._tokens = ServiceAccountTokens(client, credentials, SCOPE_CLOUD) if credentials else None
        self._timeout = timeout_seconds

    @property
    def processor_name(self) -> str:
        return f"projects/{self._project}/locations/{self._location}/processors/{self._processor}"

    def config_error(self) -> str | None:
        if not self._project:
            return "DOCUMENTAI_PROJECT_ID is not set"
        if not self._location:
            return "DOCUMENTAI_LOCATION is not set"
        if not self._processor:
            return "DOCUMENTAI_PROCESSOR_ID is not set"
        if self._tokens is None:
            return "Document AI credentials are not set"
        return None

    async def scan(self, image: bytes, mime_type: str) -> OcrResult:
        problem = self.config_error()
        if problem is not None:
            logger.error("rt_ocr_config_error", problem=problem)
            return OcrResult(error=OcrError.INVALID_CONFIG, message=problem)
        assert self._tokens is not None  # noqa: S101 - config_error checked it
        url = f"https://{self._location}-documentai.googleapis.com/v1/{self.processor_name}:process"
        body = {"rawDocument": {"content": base64.b64encode(image).decode(), "mimeType": mime_type}}
        started = time.perf_counter()
        response: httpx.Response | None = None
        try:
            for _ in range(2):  # a second try only after a 401 (stale token)
                token = await self._tokens.get()
                response = await self._client.post(
                    url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=self._timeout
                )
                if response.status_code != 401:
                    break
                self._tokens.invalidate()
        except httpx.TimeoutException:
            logger.error("rt_ocr_failed", code="TIMEOUT", ms=_ms(started))
            return OcrResult(
                error=OcrError.TIMEOUT, message=f"Request timed out after {int(self._timeout * 1000)} ms"
            )
        except httpx.HTTPError as exc:
            logger.error("rt_ocr_failed", code="API_ERROR", error=type(exc).__name__, ms=_ms(started))
            return OcrResult(
                error=OcrError.API_ERROR, message="Document AI API call failed. Please try again."
            )

        assert response is not None  # noqa: S101 - the loop ran at least once
        elapsed = _ms(started)
        if not response.is_success:
            logger.error("rt_ocr_failed", code="API_ERROR", status=response.status_code, ms=elapsed)
            return OcrResult(
                error=OcrError.API_ERROR, message="Document AI API call failed. Please try again."
            )
        try:
            document = response.json().get("document")
        except ValueError:
            document = None
        if not document:
            logger.error("rt_ocr_empty_document", ms=elapsed)
            return OcrResult(error=OcrError.PARSE_ERROR, message="Document AI returned an empty response")

        data = normalize_document(document, elapsed, self.processor_name)
        logger.info(
            "rt_ocr_ok",
            ms=elapsed,
            entities=len(document.get("entities") or []),
            fields_present={
                k: data[k]["value"] is not None for k in ("name", "license_number", "dob", "expiry_date")
            },
        )
        return OcrResult(data=data)


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
