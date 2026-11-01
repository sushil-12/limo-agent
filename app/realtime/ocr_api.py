"""``POST /rt/v1/scan-license``: replaces limoRTService's ``/api/scan-license``.

Same input (multipart field ``image``, or JSON ``{base64, mimeType}``) and the same JSON
output, so the apps only change the URL. Auth changes from the shared secret to the
caller's Passport token: drivers (registration) and admins (adding an affiliate).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from app.core.responses import ORJSONResponse
from app.realtime.deps import ForbiddenError, RtDep, UserDep
from app.realtime.ocr.documentai import HTTP_STATUS
from app.realtime.ocr.images import decode_base64_image, validate_image

router = APIRouter(prefix="/rt/v1", tags=["realtime"])

SCAN_PATH = "/rt/v1/scan-license"
ALLOWED_ROLES = frozenset({"driver", "admin"})


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _error(status: int, message: str, **extra: Any) -> ORJSONResponse:
    return ORJSONResponse(
        {"success": False, "error": message, "timestamp": _now(), **extra}, status_code=status
    )


@router.post("/scan-license")
async def scan_license(request: Request, rt: RtDep, user: UserDep) -> ORJSONResponse:
    if user.role not in ALLOWED_ROLES:
        raise ForbiddenError("Only drivers and admins can scan a licence.")
    limit = await rt.ocr_limiter.hit(f"ocr:{user.id}")
    if not limit.allowed:
        return ORJSONResponse(
            {
                "success": False,
                "error": "Too many scan requests. Please wait a minute before retrying.",
                "timestamp": _now(),
            },
            status_code=429,
            headers={"Retry-After": str(limit.retry_after_seconds)},
        )

    image, declared = await _read_image(request)
    if isinstance(image, ORJSONResponse):
        return image

    max_bytes = rt.settings.documentai_max_file_size_bytes
    check = validate_image(image, max_bytes, declared)
    if not check.valid or check.mime_type is None:
        return _error(400, check.error or "Invalid image", detected_mime=check.mime_type)

    result = await rt.ocr.scan(image, check.mime_type)
    if result.error is not None:
        return _error(HTTP_STATUS[result.error], result.message or "Licence scan failed")
    return ORJSONResponse({"success": True, "timestamp": _now(), "data": result.data})


async def _read_image(request: Request) -> tuple[bytes | ORJSONResponse, str | None]:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        try:
            form = await request.form(max_files=1, max_fields=10)
        except (MultiPartException, StarletteHTTPException) as exc:
            return _error(400, f"Upload error: {getattr(exc, 'message', None) or exc}"), None
        upload = form.get("image")
        if isinstance(upload, UploadFile):
            return await upload.read(), upload.content_type
    elif "json" in content_type:
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "Invalid JSON body"), None
        if isinstance(body, dict) and isinstance(body.get("base64"), str) and body["base64"]:
            decoded = decode_base64_image(body["base64"])
            if decoded is None:
                return _error(400, "Invalid base64 string"), None
            mime = body.get("mimeType")
            return decoded, mime if isinstance(mime, str) else None
    return (
        _error(
            400, 'No image provided. Send a file in the "image" field (multipart) or a "base64" field (JSON).'
        ),
        None,
    )
