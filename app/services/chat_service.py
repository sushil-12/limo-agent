"""Chat orchestration: the single place routes call for conversations.

Before the real reply, a turn may be answered with a fixed message instead:
- ``ask_name``: a guest who has not given a name yet is asked for it (no LLM call).
- ``off_topic``: the topic guard classified the message as outside 1800Limo.

For users who may query data, the same guard call can answer DATA: the data agent
then writes and runs a guarded SQL query, and the reply is generated from its result
(``reply_type="data"``; the streamed ``data`` event carries the rows). A question the
data agent has answered before skips the guard and goes straight to the agent.

With ``CHAT_LOG_PATH`` set, every turn (question, reply, outcome, timings) is
appended to a JSONL chat log, keyed by request id and conversation id.

Future extension points (all inside this class, so routes and the API contract
stay unchanged):
- RAG: retrieve FAQ passages before calling the provider and add them to the
  system context.
- Tools: route write intents (create/cancel bookings, driver registration) to a
  provider with tool calling, via ``app.llm.factory``.
- History: load and save turns by ``conversation_id`` (e.g. Redis) instead of
  trusting client-sent history.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import structlog

from app.core.errors import (
    AppError,
    AssistantBadResponseError,
    AssistantTimeoutError,
    AssistantUnavailableError,
)
from app.core.logging import get_logger
from app.llm.base import (
    LLMConnectionError,
    LLMError,
    LLMMessage,
    LLMModelNotFoundError,
    LLMProvider,
    LLMTimeoutError,
    StreamDelta,
    Timings,
    Usage,
)
from app.schemas.chat import (
    ChatMessageIn,
    ChatRequest,
    ChatResponse,
    DataOut,
    GuestChatRequest,
    TimingsOut,
    UsageOut,
)
from app.services.context_window import fit_history
from app.services.data.agent import DataAgent, DataResult
from app.services.data.followups import is_place_question, standalone_questions
from app.services.data.money import is_stripe_question
from app.services.data.people import answers_menu, parse_person_question
from app.services.data.query_log import JsonlLog
from app.services.data.templates import template_sql
from app.services.prompts import (
    ASK_NAME_REPLY,
    OFF_TOPIC_REPLY,
    build_data_context,
    build_data_reply,
    build_system_prompt,
)
from app.services.requester import Requester
from app.services.topic_guard import TopicGuard

logger = get_logger(__name__)

ReplyType = Literal["answer", "data", "ask_name", "off_topic"]
_NO_USAGE = UsageOut(prompt_tokens=0, completion_tokens=0)
_FOLLOW_UP = re.compile(
    r"^\W*(and|also|but|or|only|just|now|then|same|those|them|these|what about|how about|what of|vs)\b",
    re.IGNORECASE,
)
_FOLLOW_UP_MAX_WORDS = 8
# Clear record questions skip the router model, which sometimes answers ON for them.
_RECORD_WORD = re.compile(
    r"\b(bookings?|reservations?|rides?|trips?|drivers?|affiliates?|passengers?|pick ?ups?|drop ?offs?|"
    r"revenue|invoices?|orders?|earnings?|profits?|income|commissions?|payouts?|expenses?|spent|tax(es)?|"
    r"stripe)\b",
    re.IGNORECASE,
)
_CONTEXT_QUESTIONS = 2  # earlier user questions the topic check sees
# Words pointing at the previous answer's rows: "name those", "their numbers".
_REFERS_BACK = re.compile(
    r"\b(them|those|these|they|their|there|names?|numbers?|phones?|contacts?|details|emails?)\b",
    re.IGNORECASE,
)
_ASKS_FOR_RECORDS = re.compile(
    r"^\W*(how many|how much|which|who|what|when|where|list|show|tell|give|find|count|total|any)\b|"
    r"\b(my|upcoming|recent|latest|last|next|pending|cancelled|unassigned|today|yesterday|tomorrow|"
    r"this (week|month|year))\b|#?\d{3,}",
    re.IGNORECASE,
)
_ASKS_HOW_TO = re.compile(
    r"\b(how (do|can|to|does)|can i|could i|should i|i want to|i'd like to|help me|policy|cost|allowed)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ChatStarted:
    conversation_id: str
    model: str


@dataclass(frozen=True, slots=True)
class ChatDelta:
    content: str


@dataclass(frozen=True, slots=True)
class ChatData:
    """The database lookup a data answer is based on; sent before the text."""

    data: DataOut


@dataclass(frozen=True, slots=True)
class ChatDone:
    usage: UsageOut
    timings: TimingsOut
    reply_type: ReplyType = "answer"


@dataclass(frozen=True, slots=True)
class ChatFailed:
    code: str
    message: str


ChatEvent = ChatStarted | ChatData | ChatDelta | ChatDone | ChatFailed


@dataclass(frozen=True, slots=True)
class _Canned:
    reply_type: ReplyType
    content: str


_DATA_ROUTE = "data"


def to_app_error(exc: LLMError) -> AppError:
    """Translate a provider failure into a client-safe error (no upstream details)."""
    if isinstance(exc, LLMConnectionError | LLMModelNotFoundError):
        return AssistantUnavailableError()
    if isinstance(exc, LLMTimeoutError):
        return AssistantTimeoutError()
    return AssistantBadResponseError()


class ChatService:
    """Builds the prompt, calls the provider, and records metrics."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        num_ctx: int,
        reserved_for_reply: int,
        topic_guard: TopicGuard | None = None,
        guest_max_messages: int = 10,
        data_agent: DataAgent | None = None,
        chat_log: JsonlLog | None = None,
        log_content: bool = False,
    ) -> None:
        self._provider = provider
        self._num_ctx = num_ctx
        self._reserved = reserved_for_reply
        self._guard = topic_guard
        self._guest_max_messages = guest_max_messages
        self._data = data_agent
        self._chat_log = chat_log
        self._log_content = log_content

    @property
    def model(self) -> str:
        """Model name used for replies."""
        return self._provider.model

    def _history(self, requester: Requester, request: ChatRequest) -> list[ChatMessageIn]:
        """Client-sent history; guests keep only their newest messages."""
        messages = list(request.messages)
        if requester.is_guest:
            messages = messages[-self._guest_max_messages :]
            while messages[0].role != "user":
                messages.pop(0)
        return messages

    def _can_query(self, requester: Requester) -> bool:
        return self._data is not None and self._guard is not None and self._data.supports(requester)

    def _prepare(
        self, requester: Requester, history: Sequence[ChatMessageIn], data: DataResult | None = None
    ) -> list[LLMMessage]:
        context = (
            build_data_context(data.status, data.columns, data.rows, truncated=data.truncated)
            if data is not None
            else None
        )
        turns = _llm_history(history)
        if context is not None:
            # Next to the question, not in the system prompt: small models answer from the
            # latest turn and otherwise drift to numbers from earlier in the conversation.
            turns[-1] = LLMMessage("user", f"{turns[-1].content}\n\n{context}")
        messages = fit_history(
            build_system_prompt(requester, can_look_up=self._can_query(requester)),
            turns,
            num_ctx=self._num_ctx,
            reserved_for_reply=self._reserved,
        )
        if self._log_content:
            logger.debug("chat_prompt", messages=[m.content for m in messages])
        return messages

    async def _route(
        self, requester: Requester, request: ChatRequest, history: Sequence[ChatMessageIn]
    ) -> _Canned | DataResult | None:
        """A fixed reply, a data lookup result, or None to just generate. May raise LLMError."""
        if requester.is_guest and requester.display_name is None:
            return _Canned("ask_name", ASK_NAME_REPLY)
        if self._guard is None:
            return None
        if self._data is not None and self._can_query(requester):
            turns = _llm_history(history)
            if self._data.knows(requester, turns):
                return await self._data.answer(requester, turns)
        check = _message_to_check(request, history)
        if check is None:
            return None
        message, previous_reply, earlier = check
        can_query = self._can_query(requester)
        if can_query and self._data is not None:
            turns = _llm_history(history)
            # "how many in washington" after affiliates in Chicago: judged as the full question.
            asked = standalone_questions(turns)
            question = asked[-1] if asked else message
            if (
                answers_menu(turns)  # "3" picks from the latest "which one?" menu
                or is_place_question(previous_reply)  # "DC" answers "Washington DC or State?"
                or is_stripe_question(question)
                or parse_person_question(question) is not None
                or is_record_question(question)
                or is_record_question(message)
                or is_record_follow_up(question, asked[:-1])
                or template_sql(question, asked[:-1]) is not None  # "can you name those?"
            ):
                return await self._data.answer(requester, turns)
        verdict = await self._guard.classify(message, previous_reply, allow_data=can_query, earlier=earlier)
        if verdict == "off":
            return _Canned("off_topic", OFF_TOPIC_REPLY)
        if verdict == _DATA_ROUTE and self._data is not None:
            return await self._data.answer(requester, _llm_history(history))
        return None

    async def complete(self, requester: Requester, request: ChatRequest) -> ChatResponse:
        """Generate a full reply (stream=false)."""
        conversation_id = request.conversation_id or uuid.uuid4().hex
        history = self._history(requester, request)
        started = turn_started = time.perf_counter()
        try:
            route = await self._route(requester, request, history)
        except LLMError as exc:
            self._log_failure(requester, conversation_id, exc, started)
            await self._record(requester, conversation_id, request, history, turn_started, error=exc)
            raise to_app_error(exc) from exc
        if isinstance(route, _Canned):
            canned = route
            timings = self._log_canned(requester, conversation_id, request, canned, started)
            await self._record(
                requester, conversation_id, request, history, turn_started, canned.reply_type, canned.content
            )
            return ChatResponse(
                conversation_id=conversation_id,
                content=canned.content,
                model=self.model,
                usage=_NO_USAGE,
                timings=timings,
                reply_type=canned.reply_type,
            )

        guard_ms = _ms_since(started)
        data = route
        if (fixed := _fixed_data_reply(data)) is not None:
            timings = self._log_canned(requester, conversation_id, request, fixed, started)
            await self._record(
                requester, conversation_id, request, history, turn_started, "data", fixed.content, data=data
            )
            return ChatResponse(
                conversation_id=conversation_id,
                content=fixed.content,
                model=self.model,
                usage=_NO_USAGE,
                timings=timings,
                reply_type="data",
                data=_data_out(data),
            )
        messages = self._prepare(requester, history, data)
        started = time.perf_counter()
        try:
            result = await self._provider.chat(messages)
        except LLMError as exc:
            self._log_failure(requester, conversation_id, exc, started)
            await self._record(
                requester, conversation_id, request, history, turn_started, data=data, error=exc
            )
            raise to_app_error(exc) from exc
        await self._record(
            requester,
            conversation_id,
            request,
            history,
            turn_started,
            "data" if data is not None else "answer",
            result.content,
            data=data,
            usage=result.usage,
        )
        self._log_success(
            requester,
            conversation_id,
            request,
            messages,
            len(result.content),
            result.usage,
            result.timings,
            guard_ms,
            data,
        )
        return ChatResponse(
            conversation_id=conversation_id,
            content=result.content,
            model=result.model,
            usage=_usage_out(result.usage),
            timings=_timings_out(result.timings),
            reply_type="data" if data is not None else "answer",
            data=_data_out(data),
        )

    async def stream(self, requester: Requester, request: ChatRequest) -> AsyncGenerator[ChatEvent, None]:
        """Yield ChatStarted, ChatDelta*, then ChatDone or ChatFailed.

        Provider errors become a ChatFailed event because HTTP headers are already
        sent. If the consumer closes this generator (client disconnected), the
        provider stream is closed too and the cancellation is logged.
        """
        conversation_id = request.conversation_id or uuid.uuid4().hex
        history = self._history(requester, request)
        yield ChatStarted(conversation_id, self.model)

        started = turn_started = time.perf_counter()
        try:
            route = await self._route(requester, request, history)
        except LLMError as exc:
            self._log_failure(requester, conversation_id, exc, started)
            await self._record(requester, conversation_id, request, history, turn_started, error=exc)
            error = to_app_error(exc)
            yield ChatFailed(error.code, error.message)
            return
        if isinstance(route, _Canned):
            timings = self._log_canned(requester, conversation_id, request, route, started)
            await self._record(
                requester, conversation_id, request, history, turn_started, route.reply_type, route.content
            )
            yield ChatDelta(route.content)
            yield ChatDone(_NO_USAGE, timings, route.reply_type)
            return

        guard_ms = _ms_since(started)
        data = route
        data_out = _data_out(data)
        if data_out is not None:
            yield ChatData(data_out)
        if (fixed := _fixed_data_reply(data)) is not None:
            timings = self._log_canned(requester, conversation_id, request, fixed, started)
            await self._record(
                requester, conversation_id, request, history, turn_started, "data", fixed.content, data=data
            )
            yield ChatDelta(fixed.content)
            yield ChatDone(_NO_USAGE, timings, "data")
            return
        messages = self._prepare(requester, history, data)
        started = time.perf_counter()
        output_chars = 0
        parts: list[str] = []
        finished = False
        reply_type: ReplyType = "data" if data is not None else "answer"
        upstream = self._provider.stream_chat(messages)
        try:
            async for item in upstream:
                if isinstance(item, StreamDelta):
                    output_chars += len(item.content)
                    parts.append(item.content)
                    yield ChatDelta(item.content)
                else:
                    finished = True
                    await self._record(
                        requester,
                        conversation_id,
                        request,
                        history,
                        turn_started,
                        reply_type,
                        "".join(parts),
                        data=data,
                        usage=item.usage,
                    )
                    self._log_success(
                        requester,
                        conversation_id,
                        request,
                        messages,
                        output_chars,
                        item.usage,
                        item.timings,
                        guard_ms,
                        data,
                    )
                    yield ChatDone(_usage_out(item.usage), _timings_out(item.timings), reply_type)
        except LLMError as exc:
            finished = True
            self._log_failure(requester, conversation_id, exc, started)
            await self._record(
                requester,
                conversation_id,
                request,
                history,
                turn_started,
                reply="".join(parts),
                data=data,
                error=exc,
            )
            error = to_app_error(exc)
            yield ChatFailed(error.code, error.message)
        finally:
            await upstream.aclose()
            if not finished:
                logger.info(
                    "chat_cancelled",
                    **requester.log_fields(),
                    conversation_id=conversation_id,
                    output_chars=output_chars,
                    elapsed_ms=_ms_since(started),
                )
                await self._record(
                    requester,
                    conversation_id,
                    request,
                    history,
                    turn_started,
                    reply_type,
                    "".join(parts),
                    data=data,
                    cancelled=True,
                )

    async def _record(
        self,
        requester: Requester,
        conversation_id: str,
        request: ChatRequest,
        history: Sequence[ChatMessageIn],
        started: float,
        reply_type: ReplyType | None = None,
        reply: str | None = None,
        *,
        data: DataResult | None = None,
        usage: Usage | None = None,
        error: LLMError | None = None,
        cancelled: bool = False,
    ) -> None:
        """Append the turn to the chat log, if one is configured. Never raises."""
        if self._chat_log is None:
            return
        record: dict[str, Any] = {
            "request_id": structlog.contextvars.get_contextvars().get("request_id"),
            "conversation_id": conversation_id,
            **requester.log_fields(),
            "stream": request.stream,
            "question": history[-1].content if history else None,
            "reply_type": reply_type,
            "reply": reply,
            "model": self.model,
            "total_ms": _ms_since(started),
        }
        if data is not None:
            record.update(
                data_status=data.status,
                data_cached=data.cached,
                sql=data.sql,
                row_count=len(data.rows),
                query_ms=data.elapsed_ms,
            )
        if usage is not None:
            record.update(prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens)
        if error is not None:
            record["error"] = type(error).__name__
        if cancelled:
            record["cancelled"] = True
        await self._chat_log.write(record)

    def _log_success(
        self,
        requester: Requester,
        conversation_id: str,
        request: ChatRequest,
        messages: list[LLMMessage],
        output_chars: int,
        usage: Usage,
        timings: Timings,
        guard_ms: float,
        data: DataResult | None = None,
    ) -> None:
        logger.info(
            "chat_completed",
            **requester.log_fields(),
            conversation_id=conversation_id,
            reply_type="data" if data is not None else "answer",
            data_status=data.status if data is not None else None,
            stream=request.stream,
            messages_in=len(request.messages),
            messages_sent=len(messages) - 1,
            input_chars=sum(len(m.content) for m in request.messages),
            output_chars=output_chars,
            model=self.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            guard_ms=guard_ms,
            total_ms=timings.total_ms,
            load_ms=timings.load_ms,
            ttft_ms=timings.time_to_first_token_ms,
            tokens_per_sec=timings.tokens_per_sec,
        )

    @staticmethod
    def _log_canned(
        requester: Requester, conversation_id: str, request: ChatRequest, canned: _Canned, started: float
    ) -> TimingsOut:
        timings = TimingsOut(
            total_ms=_ms_since(started), load_ms=None, time_to_first_token_ms=None, tokens_per_sec=None
        )
        logger.info(
            "chat_completed",
            **requester.log_fields(),
            conversation_id=conversation_id,
            reply_type=canned.reply_type,
            stream=request.stream,
            messages_in=len(request.messages),
            input_chars=sum(len(m.content) for m in request.messages),
            total_ms=timings.total_ms,
        )
        return timings

    @staticmethod
    def _log_failure(requester: Requester, conversation_id: str, exc: LLMError, started: float) -> None:
        logger.warning(
            "chat_failed",
            **requester.log_fields(),
            conversation_id=conversation_id,
            error_type=type(exc).__name__,
            error=str(exc),
            elapsed_ms=_ms_since(started),
        )


def _message_to_check(
    request: ChatRequest, history: Sequence[ChatMessageIn]
) -> tuple[str, str | None, list[str]] | None:
    """The user message the topic guard should classify, the reply it follows, and the
    user's earlier questions (newest last) as context for short follow-ups.

    On the turn where a guest answers the name question, the name itself says nothing
    about the topic, so the question they asked before it is checked instead.
    """
    index = len(history) - 1
    if isinstance(request, GuestChatRequest) and request.guest_name is not None:
        if history[index].content.strip().casefold() == request.guest_name.strip().casefold():
            index = _previous_user_index(history, index)
            if index is None:
                return None
    previous = history[index - 1] if index > 0 and history[index - 1].role == "assistant" else None
    message = history[index].content
    if is_follow_up(message) and (asked := _previous_user_index(history, index)) is not None:
        # "and cancelled?" says nothing on its own; with the question before it the
        # guard sees "how many pending bookings? and cancelled?" and routes it to a lookup.
        message = f"{history[asked].content}\n{message}"
    earlier = [m.content for m in history[:index] if m.role == "user"][-_CONTEXT_QUESTIONS:]
    return message, previous.content if previous is not None else None, earlier


def is_follow_up(message: str) -> bool:
    """A short message that continues the previous question ("and last month?", "only paid")."""
    return len(message.split()) <= _FOLLOW_UP_MAX_WORDS and _FOLLOW_UP.match(message) is not None


def is_record_question(message: str) -> bool:
    """Clearly asks about records ("who is the driver of booking 2096"), not how to do something."""
    return (
        _RECORD_WORD.search(message) is not None
        and _ASKS_FOR_RECORDS.search(message) is not None
        and _ASKS_HOW_TO.search(message) is None
    )


def is_record_follow_up(message: str, earlier: Sequence[str]) -> bool:
    """A short message about the rows the previous record question returned:
    "can you name those?", "their phone numbers", "list them"."""
    return (
        bool(earlier)
        and len(message.split()) <= _FOLLOW_UP_MAX_WORDS
        and _REFERS_BACK.search(message) is not None
        and is_record_question(earlier[-1])
    )


def _previous_user_index(history: Sequence[ChatMessageIn], before: int) -> int | None:
    for index in range(before - 1, -1, -1):
        if history[index].role == "user":
            return index
    return None


def _llm_history(history: Sequence[ChatMessageIn]) -> list[LLMMessage]:
    return [LLMMessage(m.role, m.content) for m in history]  # type: ignore[arg-type]


def _fixed_data_reply(data: DataResult | None) -> _Canned | None:
    """Records and tables are written out without the model (see ``build_data_reply``)."""
    if data is None:
        return None
    if data.message_only and data.message is not None:
        return _Canned("data", data.message)
    text = build_data_reply(
        data.status, data.columns, data.rows, truncated=data.truncated, heading=data.message
    )
    return _Canned("data", text) if text is not None else None


def _data_out(data: DataResult | None) -> DataOut | None:
    if data is None:
        return None
    return DataOut(
        status=data.status,
        sql=data.sql if data.show_sql else None,
        columns=data.columns,
        rows=data.rows,
        row_count=len(data.rows),
        truncated=data.truncated,
        elapsed_ms=data.elapsed_ms,
    )


def _usage_out(usage: Usage) -> UsageOut:
    return UsageOut(prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens)


def _timings_out(timings: Timings) -> TimingsOut:
    return TimingsOut(
        total_ms=timings.total_ms,
        load_ms=timings.load_ms,
        time_to_first_token_ms=timings.time_to_first_token_ms,
        tokens_per_sec=timings.tokens_per_sec,
    )


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
