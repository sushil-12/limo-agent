"""Booking a ride from the web chat: one BookingFlow per signed-in user and conversation.

A chat message that asks to book a ride ("book 5 am pickup on 14 nov from ord to mdw")
starts a flow; every later message in the same conversation goes to that flow until it is
booked, cancelled, or left idle for ``ttl_seconds``. The flow calls the 1800limo API with
the user's own Passport token, so the API applies the same role scoping as the web app.

The rules are the shell's (BOOKING_FLOW.md): only missing details are asked, nothing is
booked without the full summary and an explicit "yes", and a submit is never retried.
A turn runs to the end even if the client disconnects mid-way; whatever it said is then
sent with the next reply instead of being lost.

Sessions live in this process. Running several workers needs sticky sessions (or a shared
store) so a conversation's messages reach the same worker.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.logging import get_logger
from app.services.booking.api import BOOKING_MODES, LimoApi
from app.services.booking.flow import BookingFlow
from app.services.booking.maps import GoogleMaps
from app.services.booking.reference import AirportIndex
from app.services.requester import Requester

logger = get_logger(__name__)

# A chat line that asks to book a ride starts the booking flow instead of the chatbot.
BOOKING_REQUEST = re.compile(
    r"^\s*(?:hi|hello|hey|please|pls|kindly|can\s+you|could\s+you|i\s+(?:want|need|would\s+like)|"
    r"we\s+need|mujhe|muje|hume)?[\s,]*(?:to\s+)?(?:book|booking|reserve)\b.*\b(?:ride|car|cab|limo|taxi|"
    r"transfer|trip|pickup|pick\s*up|sedan|suv|from|se)\b|"
    r"\b(?:gaadi|gadi|cab|car|ride|taxi)\s+(?:book\s+)?(?:chahiye|karo|kar\s+do|book\s+karo)\b",
    re.IGNORECASE,
)

SIGN_IN_REPLY = "Please sign in to book a ride from the chat."
ROLE_REPLY = "Rides can't be booked from the chat with this account. Please use the website or app."
UNAVAILABLE_REPLY = "Booking from the chat isn't available right now. Please book on the website or app."
FAILED_REPLY = (
    "Sorry, something went wrong, so I stopped this booking. If you had already confirmed it, "
    "check your bookings before trying again."
)

_YES_NO = ("Yes", "No")
_NUMBERED = re.compile(r"^\s*\d+\.\s")


def is_booking_request(text: str) -> bool:
    return BOOKING_REQUEST.search(text) is not None


@dataclass(frozen=True, slots=True)
class BookingReply:
    """One booking turn for the chat UI.

    ``cards`` replace the numbered category / vehicle list in ``content``: picking a card
    sends its ``number``. ``replies`` are suggested answers to show as buttons.
    """

    content: str
    active: bool  # the conversation's next message goes to the booking
    cards_kind: str | None = None  # "category" | "vehicle"
    cards: tuple[dict[str, Any], ...] = ()
    replies: tuple[str, ...] = ()


@dataclass
class _Session:
    flow: BookingFlow
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    lines: list[str] = field(default_factory=list)
    cards_kind: str | None = None  # the latest cards shown; sent again while their menu is open
    cards: list[dict[str, Any]] = field(default_factory=list)
    used: float = 0.0

    def say(self, line: str) -> None:
        for part in line.split("\n"):
            self.lines.append(part.removeprefix("  "))

    def show_cards(self, kind: str, cards: list[dict[str, Any]]) -> None:
        # The cards stand in for the numbered lines the flow has just said.
        tail = self.lines[-len(cards) :] if cards else []
        if cards and len(tail) == len(cards) and all(_NUMBERED.match(line) for line in tail):
            del self.lines[-len(cards) :]
        self.cards_kind, self.cards = kind, cards

    def take_reply(self) -> BookingReply:
        flow = self.flow
        # A reply on an open category / vehicle menu ("sorry, I didn't get that", "summary") carries
        # its cards again: only the latest message's cards can be picked in the widget.
        menu_open = flow.menu is not None and flow.menu.kind == self.cards_kind
        reply = BookingReply(
            content="\n".join(self.lines).strip() or "OK.",
            active=not flow.finished,
            cards_kind=self.cards_kind if menu_open else None,
            cards=tuple(self.cards) if menu_open else (),
            replies=() if flow.finished else _suggested_replies(flow),
        )
        self.lines = []
        return reply


class BookingSessions:
    """Active chat bookings, keyed by user id and conversation id."""

    def __init__(
        self,
        *,
        api_factory: Callable[[str], LimoApi],
        maps: GoogleMaps | None,
        airports: Callable[[], AirportIndex],
        ttl_seconds: float = 1800.0,
        max_sessions: int = 1000,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api_factory = api_factory
        self._maps = maps
        self._airports = airports
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._now = now
        self._clock = clock
        self._sessions: dict[str, _Session] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def offers(self, requester: Requester) -> bool:
        """Whether this user can book from the chat (signed in, a booking role, Google configured)."""
        return not requester.is_guest and requester.role in BOOKING_MODES and self._maps is not None

    def active(self, requester: Requester, conversation_id: str | None) -> bool:
        key = _key(requester, conversation_id)
        return key is not None and self._live(key) is not None

    async def handle(
        self, requester: Requester, conversation_id: str | None, token: str | None, text: str
    ) -> BookingReply | None:
        """The booking's reply to ``text``, or None when this message is not part of a booking."""
        key = _key(requester, conversation_id)
        session = self._live(key) if key is not None else None
        if session is None:
            if not is_booking_request(text):
                return None
            refusal = self._refusal(requester, token)
            if refusal is not None:
                return BookingReply(refusal, active=False)
            if key is None or token is None:  # _refusal covers both; this narrows the types
                return BookingReply(SIGN_IN_REPLY, active=False)
            session = self._start(key, requester, token)
            logger.info("booking_started", **requester.log_fields(), mode=session.flow.mode)
            return await self._turn(key, session, session.flow.start(text))
        if token is not None:
            session.flow.api = self._api_factory(token)  # the client may have refreshed its token
        return await self._turn(key, session, session.flow.handle(text))

    def _refusal(self, requester: Requester, token: str | None) -> str | None:
        if requester.is_guest or token is None:
            return SIGN_IN_REPLY
        if requester.role not in BOOKING_MODES:
            return ROLE_REPLY
        if self._maps is None:
            logger.warning("booking_unavailable", reason="GOOGLE_MAPS_API_KEY is not set")
            return UNAVAILABLE_REPLY
        return None

    def _start(self, key: str, requester: Requester, token: str) -> _Session:
        self._prune()
        mode = BOOKING_MODES[requester.role]
        flow = BookingFlow(
            mode=mode,
            user_id=requester.user_id or 0,
            api=self._api_factory(token),
            maps=self._maps,  # type: ignore[arg-type]  # _refusal returned for None
            airports=self._airports(),
            card_form=False,
        )
        if self._now is not None:
            flow.now = self._now
        session = _Session(flow=flow, used=self._clock())
        flow.say, flow.show_cards = session.say, session.show_cards
        self._sessions[key] = session
        return session

    async def _turn(self, key: str, session: _Session, action: Any) -> BookingReply:
        async def run() -> None:
            async with session.lock:
                await action

        task = asyncio.ensure_future(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            # Shielded: a client that disconnects mid-turn must not cut a submit in half.
            await asyncio.shield(task)
        except Exception:
            logger.exception("booking_failed")
            self._sessions.pop(key, None)
            return BookingReply(FAILED_REPLY, active=False)
        async with session.lock:
            session.used = self._clock()
            reply = session.take_reply()
        if not reply.active:
            self._sessions.pop(key, None)
        return reply

    def _live(self, key: str) -> _Session | None:
        session = self._sessions.get(key)
        if session is not None and self._clock() - session.used > self._ttl:
            self._sessions.pop(key, None)
            return None
        return session

    def _prune(self) -> None:
        cutoff = self._clock() - self._ttl
        for key in [k for k, s in self._sessions.items() if s.used < cutoff]:
            del self._sessions[key]
        while len(self._sessions) >= self._max:
            oldest = min(self._sessions, key=lambda k: self._sessions[k].used)
            del self._sessions[oldest]


def _key(requester: Requester, conversation_id: str | None) -> str | None:
    if requester.user_id is None:
        return None
    return f"{requester.user_id}:{conversation_id or ''}"


def _suggested_replies(flow: BookingFlow) -> tuple[str, ...]:
    """Buttons for the question the flow is waiting on; numbered menus are picked by number."""
    pending = flow.pending or ""
    menu = flow.menu.kind if flow.menu is not None else ""
    replies: tuple[str, ...] = ()
    if pending in ("confirm", "relative", "return_route", "card"):
        replies = _YES_NO
    elif pending == "service":
        replies = ("One way", "Round trip", "Charter")
    elif pending in ("ampm", "return_ampm"):
        replies = ("AM", "PM")
    elif pending.startswith("meet_default:"):
        replies = ("OK",)
    elif pending.endswith((":dropoff_flight", ":cruise_time", ":instructions")):
        replies = ("Skip",)
    elif pending in ("stops", "return_stops"):
        replies = ("No",)
    if menu == "vehicle":
        replies = (*replies, "Back")
    return (*replies, "Cancel")
