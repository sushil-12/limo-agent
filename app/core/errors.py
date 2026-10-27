"""Error types and handlers producing one consistent JSON error body.

Every error response looks like::

    {"error": {"code": "...", "message": "...", "request_id": "..."}}
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger
from app.core.responses import ORJSONResponse

logger = get_logger(__name__)


class AppError(Exception):
    """Base class for errors that map to a JSON error response."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.headers = headers
        super().__init__(self.message)


class UnauthorizedError(AppError):
    """Missing or invalid credentials. The message never says which check failed."""

    status_code = 401
    code = "unauthorized"
    message = "Invalid or missing credentials."

    def __init__(self) -> None:
        super().__init__(headers={"WWW-Authenticate": "Bearer"})


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    message = "Resource not found."


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"
    message = "Request body is too large."


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"
    message = "Too many requests. Please slow down."

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(headers={"Retry-After": str(max(1, retry_after_seconds))})


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"
    message = "A required service is temporarily unavailable."


class AssistantUnavailableError(AppError):
    status_code = 503
    code = "llm_unavailable"
    message = "The assistant is temporarily unavailable."


class AssistantTimeoutError(AppError):
    status_code = 504
    code = "llm_timeout"
    message = "The assistant took too long to respond."


class AssistantBadResponseError(AppError):
    status_code = 502
    code = "llm_error"
    message = "The assistant returned an error."


_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
}


def request_id_of(request: Request) -> str | None:
    """Return the id assigned by RequestContextMiddleware."""
    return getattr(request.state, "request_id", None)


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
    details: list[dict[str, Any]] | None = None,
) -> ORJSONResponse:
    """Build the standard error response."""
    body: dict[str, Any] = {"code": code, "message": message, "request_id": request_id_of(request)}
    if details is not None:
        body["details"] = details
    return ORJSONResponse({"error": body}, status_code=status_code, headers=headers)


async def _app_error_handler(request: Request, exc: AppError) -> ORJSONResponse:
    return error_response(request, exc.status_code, exc.code, exc.message, headers=exc.headers)


async def _http_error_handler(request: Request, exc: StarletteHTTPException) -> ORJSONResponse:
    code = _STATUS_CODES.get(exc.status_code, "error")
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    headers = dict(exc.headers) if exc.headers else None
    return error_response(request, exc.status_code, code, message, headers=headers)


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> ORJSONResponse:
    # Never echo "input": it can contain the user's message content.
    details = [
        {"loc": [str(part) for part in err.get("loc", ())], "message": err.get("msg", "")}
        for err in exc.errors()
    ]
    # Logged so a 422 in the access log can be explained: which fields and why, never the values.
    logger.warning(
        "request_invalid",
        method=request.method,
        path=request.url.path,
        errors=[f"{'.'.join(d['loc'])}: {d['message']}" for d in details],
    )
    return error_response(request, 422, "validation_error", "The request is invalid.", details=details)


async def _unhandled_error_handler(request: Request, exc: Exception) -> ORJSONResponse:
    logger.exception("unhandled_error", error_type=type(exc).__name__)
    return error_response(request, 500, "internal_error", AppError.message)


def register_exception_handlers(app: FastAPI) -> None:
    """Install the handlers that produce the standard error body."""
    app.add_exception_handler(AppError, _app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_error_handler)
