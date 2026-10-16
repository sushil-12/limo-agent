"""ASGI middleware that is not tied to logging."""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import PayloadTooLargeError
from app.core.responses import ORJSONResponse


class BodySizeLimitMiddleware:
    """Reject request bodies larger than ``max_bytes``.

    Checks Content-Length up front and also counts streamed (chunked) bytes, so a
    client cannot bypass the limit by omitting the header.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
                break

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # FastAPI re-raises HTTPException from body parsing (anything else
                    # becomes a 400); the error handler renders the standard body.
                    raise HTTPException(413, PayloadTooLargeError.message)
            return message

        await self.app(scope, limited_receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        # Runs outside FastAPI's exception handlers, so build the error body here.
        error = PayloadTooLargeError()
        request_id = scope.get("state", {}).get("request_id")
        body = {"error": {"code": error.code, "message": error.message, "request_id": request_id}}
        await ORJSONResponse(body, status_code=error.status_code)(scope, receive, send)
