"""Booking a ride from the web chat: one BookingFlow per signed-in user and conversation.

A chat message that asks to book a ride ("book 5 am pickup on 14 nov from ord to mdw")
starts a flow, and one that asks to change a booking ("edit booking 2141") starts an
EditFlow (EDIT_BOOKING_FLOW.md); every later message in the same conversation goes to that
flow until it is booked or saved, cancelled, or left idle for ``ttl_seconds``. The flow calls
the 1800limo API with the user's own Passport token, so the API applies the same role scoping
as the web app.

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
from app.services.booking.edit import EditFlow, is_edit_follow_up, is_edit_request
from app.services.booking.flow import BookingFlow
from app.services.booking.lookup import ReservationLookup
from app.services.booking.maps import GoogleMaps
from app.services.booking.reference import AirportIndex
from app.services.booking.understand import TripReader
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
# "help me create a booking", "i need to make a reservation for ...", "i want to book another".
NEW_BOOKING_REQUEST = re.compile(
    r"\b(?:create|make|place|start|do|set\s+up)\s+(?:(?:a|an|the|my|one|new|another)\s+)*"
    r"(?:bookings?|reservations?|rides?)\b|"
    r"\b(?:book|reserve)\s+(?:a|an|another|me|us|one)\b|"
    r"\bhelp\s+me\s+(?:to\s+)?(?:book|reserve)\b",
    re.IGNORECASE,
)
# How-to and lookup questions ("how do I make a booking?", "did I book a ride yesterday?") stay
# with the chatbot.
_QUESTION = re.compile(
    r"^\s*(?:how|what|why|when|where|which|who|whom|did|does|do|is|are|was|were|have|has)\b",
    re.IGNORECASE,
)

SIGN_IN_REPLY = "Please sign in to book a ride from the chat."
ROLE_REPLY = "Rides can't be booked from the chat with this account. Please use the website or app."
UNAVAILABLE_REPLY = "Booking from the chat isn't available right now. Please book on the website or app."
EDIT_SIGN_IN_REPLY = "Please sign in to change a booking from the chat."
EDIT_ROLE_REPLY = "Bookings can't be changed from the chat with this account. Please use the website or app."
EDIT_UNAVAILABLE_REPLY = (
    "Changing a booking from the chat isn't available right now. Please use the website or app."
)
INTERRUPTED_REPLY = (
    "Sorry, that booking was interrupted before it was finished, so nothing was booked. "
    "Please start again with the whole trip in one message, for example: "
    "book a ride from MDW to ORD on 14 nov at 5 pm."
)
# The booking's own questions ("What pickup time?", "How many bags?"). When the previous reply
# ended with one but no booking is open (it went idle, or the server restarted), the answer
# must not reach the chatbot: it would play along and "confirm" a ride nobody booked.
_OPEN_QUESTION = re.compile(
    r"^(?:What (?:date|pickup time)|Where (?:should|to|is the|does the return)|Which (?:terminal|city|date|"
    r"stop|country)|How many (?:passengers|bags|hours)|One way, round trip|Return (?:date|pickup|trip)|"
    r"Passenger name|Phone number for|Any (?:special instructions|extra stops)|Added (?:return )?stop|"
    r"(?:Arriving|Departing) .*flight number|"
    r"(?:Customer|Client|Travel agent) name|New customer's|Cruise|Ship time|Add a payment card|"
    r"Confirm booking|Pick (?:a|one)|'.+' = .+Right)",
    re.IGNORECASE,
)
FAILED_REPLY = (
    "Sorry, something went wrong, so I stopped this booking. If you had already confirmed it, "
    "check your bookings before trying again."
)

_YES_NO = ("Yes", "No")
_NUMBERED = re.compile(r"^\s*\d+\.\s")


def is_booking_request(text: str) -> bool:
    if BOOKING_REQUEST.search(text) is not None:
        return True
    return NEW_BOOKING_REQUEST.search(text) is not None and _QUESTION.match(text) is None


def asked_booking_question(reply: str) -> bool:
    """Whether ``reply`` ends with one of the booking's questions (or its numbered options)."""
    lines = [line.strip() for line in reply.splitlines() if line.strip()]
    while lines and _NUMBERED.match(lines[-1]):
        lines.pop()
    return bool(lines) and _OPEN_QUESTION.match(lines[-1]) is not None


@dataclass(frozen=True, slots=True)
class BookingReply:
    """One booking turn for the chat UI.

    ``cards`` replace the numbered category / vehicle list in ``content``: picking a card
    sends its ``number``. ``options`` replace any other numbered list (places, airports,
    terminals, customers...) the same way, as buttons ``{"number", "label"}``. ``replies`` are
    suggested answers to show as buttons.
    """

    content: str
    active: bool  # the conversation's next message goes to the booking
    cards_kind: str | None = None  # "category" | "vehicle"
    cards: tuple[dict[str, Any], ...] = ()
    options: tuple[dict[str, Any], ...] = ()
    replies: tuple[str, ...] = ()
    # A changed booking for a new customer: the widget's card form adds the card to ``payload``
    # and sends it to the 1800limo API itself (EditFlow), so the card never reaches this server.
    card_request: dict[str, Any] | None = None
    preview: dict[str, Any] | None = None  # the booking to confirm, shown as a designed card
    booked: dict[str, Any] | None = None  # the booking just created, shown as a success card


@dataclass
class _Session:
    flow: BookingFlow
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    lines: list[str] = field(default_factory=list)
    cards_kind: str | None = None  # the latest cards shown; sent again while their menu is open
    cards: list[dict[str, Any]] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)  # the open menu's buttons
    options_menu: object = None  # the menu they belong to; sent again while it is open
    preview: dict[str, Any] | None = None  # sent once, with the reply that asks to confirm
    booked: dict[str, Any] | None = None  # sent once, with the reply that says it is booked
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

    def show_preview(self, preview: dict[str, Any]) -> None:
        self.preview = preview

    def show_booked(self, booked: dict[str, Any]) -> None:
        self.booked = booked

    def take_reply(self) -> BookingReply:
        flow = self.flow
        # A reply on an open category / vehicle menu ("sorry, I didn't get that", "summary") carries
        # its cards again: only the latest message's cards can be picked in the widget.
        menu_open = flow.menu is not None and flow.menu.kind == self.cards_kind
        if flow.menu is not None and not menu_open:
            self._take_options(len(flow.menu.options))
        options_open = flow.menu is not None and flow.menu is self.options_menu
        card_request = getattr(flow, "card_request", None)
        reply = BookingReply(
            content="\n".join(self.lines).strip() or "OK.",
            active=not flow.finished,
            cards_kind=self.cards_kind if menu_open else None,
            cards=tuple(self.cards) if menu_open else (),
            options=tuple(self.options) if options_open and not menu_open else (),
            replies=() if flow.finished else _suggested_replies(flow),
            card_request=card_request if not flow.finished else None,
            preview=self.preview,
            booked=self.booked,
        )
        if isinstance(flow, EditFlow):
            flow.card_request = None  # sent once; the flow sets it again to show the form again
        self.lines = []
        self.preview = self.booked = None
        return reply

    def _take_options(self, count: int) -> None:
        """Turn the open menu's "1. ..." to "<count>. ..." lines into buttons."""
        numbers = [i for i, line in enumerate(self.lines) if line.startswith("1. ")]
        for start in reversed(numbers):
            block = self.lines[start : start + count]
            if len(block) == count and all(line.startswith(f"{n}. ") for n, line in enumerate(block, 1)):
                self.options = [
                    {"number": n, "label": line.split(". ", 1)[1].strip()} for n, line in enumerate(block, 1)
                ]
                self.options_menu = self.flow.menu
                del self.lines[start : start + count]
                return


class BookingSessions:
    """Active chat bookings, keyed by user id and conversation id."""

    def __init__(
        self,
        *,
        api_factory: Callable[[str], LimoApi],
        maps: GoogleMaps | None,
        airports: Callable[[], AirportIndex],
        lookup: ReservationLookup | None = None,
        reader: TripReader | None = None,
        ttl_seconds: float = 1800.0,
        max_sessions: int = 1000,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api_factory = api_factory
        self._maps = maps
        self._airports = airports
        self._lookup = lookup
        self._reader = reader
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._now = now
        self._clock = clock
        self._sessions: dict[str, _Session] = {}
        # The booking each conversation last made or edited, so "change the customer" right after
        # reaches the edit flow instead of the language model (which would only pretend to change it).
        self._recent: dict[str, tuple[int, float]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def offers(self, requester: Requester) -> bool:
        """Whether this user can book from the chat (signed in, a booking role, Google configured)."""
        return not requester.is_guest and requester.role in BOOKING_MODES and self._maps is not None

    def active(self, requester: Requester, conversation_id: str | None) -> bool:
        key = _key(requester, conversation_id)
        return key is not None and self._live(key) is not None

    async def handle(
        self,
        requester: Requester,
        conversation_id: str | None,
        token: str | None,
        text: str,
        previous_reply: str | None = None,
    ) -> BookingReply | None:
        """The booking's reply to ``text``, or None when this message is not part of a booking.
        ``previous_reply`` is the assistant's last message in the client's history."""
        key = _key(requester, conversation_id)
        session = self._live(key) if key is not None else None
        if session is None:
            edit = is_edit_request(text)
            recent = self._recent_booking(key) if key is not None else None
            follow_up = recent if not edit and recent is not None and is_edit_follow_up(text) else None
            edit = edit or follow_up is not None
            if not edit and not is_booking_request(text):
                if previous_reply is not None and asked_booking_question(previous_reply):
                    logger.info("booking_interrupted", **requester.log_fields())
                    return BookingReply(INTERRUPTED_REPLY, active=False)
                return None
            refusal = self._refusal(requester, token, edit=edit)
            if refusal is not None:
                return BookingReply(refusal, active=False)
            if key is None or token is None:  # _refusal covers both; this narrows the types
                return BookingReply(EDIT_SIGN_IN_REPLY if edit else SIGN_IN_REPLY, active=False)
            session = self._start(key, requester, token, edit=edit)
            event = "booking_edit_started" if edit else "booking_started"
            logger.info(event, **requester.log_fields(), mode=session.flow.mode)
            if follow_up is not None and isinstance(session.flow, EditFlow):
                return await self._turn(key, session, session.flow.start(text, reservation_id=follow_up))
            return await self._turn(key, session, session.flow.start(text))
        if token is not None:
            session.flow.api = self._api_factory(token)  # the client may have refreshed its token
        return await self._turn(key, session, session.flow.handle(text))

    def _refusal(self, requester: Requester, token: str | None, *, edit: bool = False) -> str | None:
        if requester.is_guest or token is None:
            return EDIT_SIGN_IN_REPLY if edit else SIGN_IN_REPLY
        if requester.role not in BOOKING_MODES:
            return EDIT_ROLE_REPLY if edit else ROLE_REPLY
        if self._maps is None:
            logger.warning("booking_unavailable", reason="GOOGLE_MAPS_API_KEY is not set")
            return EDIT_UNAVAILABLE_REPLY if edit else UNAVAILABLE_REPLY
        return None

    def _start(self, key: str, requester: Requester, token: str, *, edit: bool = False) -> _Session:
        self._prune()
        mode = BOOKING_MODES[requester.role]
        common: dict[str, Any] = {
            "mode": mode,
            "user_id": requester.user_id or 0,
            "api": self._api_factory(token),
            "maps": self._maps,  # _refusal returned for None
            "airports": self._airports(),
            "card_form": False,
            "reader": self._reader,
        }
        flow = EditFlow(**common, lookup=self._lookup) if edit else BookingFlow(**common)
        if self._now is not None:
            flow.now = self._now
        session = _Session(flow=flow, used=self._clock())
        flow.say, flow.show_cards = session.say, session.show_cards
        flow.show_preview, flow.show_booked = session.show_preview, session.show_booked
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
            if (reservation_id := _booking_of(session.flow)) is not None:
                self._recent[key] = (reservation_id, self._clock())
        return reply

    def _recent_booking(self, key: str) -> int | None:
        found = self._recent.get(key)
        if found is None or self._clock() - found[1] > self._ttl:
            self._recent.pop(key, None)
            return None
        return found[0]

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
        for key in [k for k, (_, used) in self._recent.items() if used < cutoff]:
            del self._recent[key]
        while len(self._sessions) >= self._max:
            oldest = min(self._sessions, key=lambda k: self._sessions[k].used)
            del self._sessions[oldest]


def _booking_of(flow: BookingFlow) -> int | None:
    """The booking a finished flow made or edited."""
    if isinstance(flow, EditFlow):
        return flow.reservation_id
    booked = flow._booked or {}
    try:
        return int(booked["reservation_id"])
    except (KeyError, TypeError, ValueError):
        return None


def _key(requester: Requester, conversation_id: str | None) -> str | None:
    if requester.user_id is None:
        return None
    return f"{requester.user_id}:{conversation_id or ''}"


def _suggested_replies(flow: BookingFlow) -> tuple[str, ...]:
    """Buttons for the question the flow is waiting on; numbered menus are picked by number."""
    pending = flow.pending or ""
    menu = flow.menu.kind if flow.menu is not None else ""
    replies: tuple[str, ...] = ()
    if pending == "passenger_choice":
        replies = ("1", "2")
    elif pending in ("confirm", "relative", "return_route", "card", "proceed"):
        replies = _YES_NO
    elif pending == "service":
        replies = ("One way", "Round trip", "Charter")
    elif pending in ("ampm", "return_ampm"):
        replies = ("AM", "PM")
    elif pending.startswith("meet_default:"):
        replies = ("OK",)
    elif pending == "detail:1:instructions" and flow.d.instructions:
        replies = ("Same", "Skip")
    elif pending.endswith((":dropoff_flight", ":cruise_time", ":instructions")):
        replies = ("Skip",)
    elif pending in ("stops", "return_stops"):
        replies = ("No",)
    if isinstance(flow, EditFlow) and flow.reservation_id is not None and pending != "proceed":
        replies = (*replies, "Undo")
    if menu == "vehicle":
        replies = (*replies, "Back")
    return (*replies, "Cancel")
