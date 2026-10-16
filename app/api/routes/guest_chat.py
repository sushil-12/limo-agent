"""Chat for visitors who are not signed in. Mounted when GUEST_CHAT_ENABLED=true."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.deps import ChatServiceDep, GuestKeyDep
from app.api.routes.chat import respond
from app.schemas.chat import ChatResponse, GuestChatRequest
from app.schemas.errors import ERROR_RESPONSES, ErrorResponse
from app.services.requester import Requester

router = APIRouter(prefix="/v1/guest", tags=["chat"])


@router.post(
    "/chat",
    response_model=ChatResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "JSON, or SSE when stream=true"},
        422: ERROR_RESPONSES[422],
        429: {"model": ErrorResponse, "description": "Rate limited (per client IP)"},
        503: {"model": ErrorResponse, "description": "Assistant unavailable"},
    },
)
async def guest_chat(
    body: GuestChatRequest, guest: GuestKeyDep, service: ChatServiceDep, request: Request
) -> ChatResponse | StreamingResponse:
    """Like /v1/chat without a token. Until ``guest_name`` is sent, the reply asks for it."""
    return await respond(service, Requester.guest(guest, body.guest_name), body, request)
