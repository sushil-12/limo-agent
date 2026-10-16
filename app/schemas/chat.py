"""Request/response contract for POST /v1/chat (see docs/API.md)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_MESSAGES = 30
MAX_MESSAGE_CHARS = 4000
CLIENT_ROLES = ("user", "assistant")


class ChatMessageIn(BaseModel):
    """One message of client-held history. Clients may not send system messages."""

    model_config = ConfigDict(extra="forbid")

    role: str
    content: Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE_CHARS)]

    @field_validator("role")
    @classmethod
    def _client_role(cls, value: str) -> str:
        if value not in CLIENT_ROLES:
            raise ValueError("role must be 'user' or 'assistant'; system messages are not allowed")
        return value

    @field_validator("content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class ChatRequest(BaseModel):
    """Body of POST /v1/chat."""

    model_config = ConfigDict(extra="forbid")

    messages: Annotated[list[ChatMessageIn], Field(min_length=1, max_length=MAX_MESSAGES)]
    stream: bool = True
    conversation_id: Annotated[str | None, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")] = None

    @model_validator(mode="after")
    def _ends_with_user(self) -> ChatRequest:
        if self.messages[-1].role != "user":
            raise ValueError("the last message must have role 'user'")
        return self


class GuestChatRequest(ChatRequest):
    """Body of POST /v1/guest/chat. ``guest_name`` is the guest's answer to the name question."""

    guest_name: Annotated[str | None, Field(max_length=100)] = None


class UsageOut(BaseModel):
    prompt_tokens: int
    completion_tokens: int


class TimingsOut(BaseModel):
    total_ms: float
    load_ms: float | None
    time_to_first_token_ms: float | None
    tokens_per_sec: float | None


class DataOut(BaseModel):
    """The database lookup behind a reply_type="data" answer."""

    # ok | no_access (role can't query) | unanswerable | failed | timeout
    status: Literal["ok", "no_access", "unanswerable", "failed", "timeout"]
    sql: str | None = None  # admins only
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool  # the row cap was reached; more rows exist
    elapsed_ms: float | None


class ChatResponse(BaseModel):
    """Body returned when stream=false."""

    conversation_id: str
    content: str
    model: str
    usage: UsageOut
    timings: TimingsOut
    # "answer" = generated reply; "data" = generated from a database lookup (see ``data``);
    # "ask_name" / "off_topic" = fixed reply, no generation.
    reply_type: Literal["answer", "data", "ask_name", "off_topic"] = "answer"
    data: DataOut | None = None
