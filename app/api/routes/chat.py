"""Chat endpoint called by the frontend widget."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.deps import ChatServiceDep, ChatUserDep
from app.api.sse import SSE_HEADERS, chat_sse
from app.auth.dependencies import bearer_token
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.errors import ERROR_RESPONSES, ErrorResponse
from app.services.chat_service import ChatService
from app.services.requester import Requester

router = APIRouter(prefix="/v1", tags=["chat"])


@router.post(
    "/chat",
    response_model=ChatResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "JSON, or SSE when stream=true"},
        **ERROR_RESPONSES,
        429: {"model": ErrorResponse, "description": "Rate limited"},
        503: {"model": ErrorResponse, "description": "Assistant unavailable"},
    },
)
async def chat(
    body: ChatRequest, user: ChatUserDep, service: ChatServiceDep, request: Request
) -> ChatResponse | StreamingResponse:
    """Reply to the conversation as JSON, or as Server-Sent Events when stream=true.

    The bearer token is passed on so a booking can call the 1800limo API as this user.
    """
    return await respond(service, Requester.from_user(user), body, request, bearer_token(request))


async def respond(
    service: ChatService,
    requester: Requester,
    body: ChatRequest,
    request: Request,
    token: str | None = None,
) -> ChatResponse | StreamingResponse:
    """Shared by the user and guest chat routes."""
    if not body.stream:
        return await service.complete(requester, body, token)
    ping_interval: float = request.app.state.settings.sse_ping_interval_seconds
    return StreamingResponse(
        chat_sse(service.stream(requester, body, token), ping_interval),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
