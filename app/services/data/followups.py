"""Follow-ups made standalone, and place names that need asking about.

A short follow-up only makes sense with the question before it: "how many in washington"
after "how many affiliates are in chicago?" means "how many affiliates are in washington?".
``standalone_questions`` rewrites each user turn of a conversation that way, so the topic
check, the templates and the SQL model all see the full question.

Some place names mean several places. "Washington" is Washington DC or Washington State,
so the question is asked back ("Do you mean Washington DC or Washington State?") and the
answer ("DC") is put into the earlier question in place of the ambiguous name.

Place slots are cut at the first word that isn't part of a place name, so "affiliates in
washington give there name and numbers" has the place "washington", not the whole tail.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.llm.base import LLMMessage


@dataclass(frozen=True, slots=True)
class Place:
    """How to match a place in addresses and badge city names (lower case, no dots or commas)."""

    label: str  # as shown to the user and put into the question: "Washington DC"
    badge: str | None  # LIKE pattern for tbl_badge_cities.name, None = no badge city for it
    address: str  # LIKE pattern for the account's address
    keywords: tuple[str, ...] = ()  # words in an answer that pick this place; codes only alone


_AMBIGUOUS: dict[str, tuple[Place, ...]] = {
    "washington": (
        Place(
            "Washington DC", "%washington dc%", "%washington dc%", ("dc", "district", "columbia", "capital")
        ),
        Place("Washington State", None, "% wa %", ("state", "wa", "seattle")),
    ),
    "portland": (
        Place("Portland Oregon", "%portland%", "%portland or %", ("oregon", "or")),
        Place("Portland Maine", None, "%portland me %", ("maine", "me")),
    ),
    "springfield": (
        Place("Springfield Illinois", None, "%springfield il %", ("illinois", "il")),
        Place("Springfield Missouri", None, "%springfield mo %", ("missouri", "mo")),
        Place("Springfield Massachusetts", None, "%springfield ma %", ("massachusetts", "ma")),
    ),
    "kansas city": (
        Place("Kansas City Missouri", "%kansas city%", "%kansas city mo %", ("missouri", "mo")),
        Place("Kansas City Kansas", None, "%kansas city ks %", ("kansas", "ks")),
    ),
}
_PREPOSITION = r"\b(?:in|near|around|from|at|based in|located in|within)\s+(?:the\s+)?"
# Words that end a place name: "in washington give their numbers", "in chicago with phones".
_NOT_PLACE = frozenset(
    {
        "give", "tell", "show", "list", "send", "share", "with", "and", "their", "there", "them",
        "they", "who", "which", "what", "that", "please", "name", "names", "number", "numbers",
        "phone", "phones", "contact", "contacts", "detail", "details", "how", "are", "is", "was",
        "were", "for", "to", "me", "us", "all", "also", "approved", "accepted", "active", "pending",
        "onboarding", "by", "area", "city", "region", "metro",
    }
)  # fmt: skip
# No quotes: templates put the place inside '...'.
_PLACE_WORD = re.compile(r"[a-z][a-z.-]*,?", re.IGNORECASE)
_MAX_PLACE_WORDS = 4
# "how many in washington", "and in dc?", "what about near dallas", "in seattle?"
_PLACE_FOLLOW_UP = re.compile(
    r"^\W*(?:and|also|ok|okay|what about|how about|same for|now)?\s*(?:how many|how much|count|number)?\s*"
    r"(?:are\s+there|are|is\s+there|is|there)?\s*(?:in|near|around|at|from)\s+(?P<place>.+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PlaceSlot:
    place: str  # as written, e.g. "Washington"
    start: int  # span of the place in the question
    end: int


def place_slot(text: str, start: int = 0) -> PlaceSlot | None:
    """The first "in <place>" after ``start``: at most four place words, stopping at other words."""
    for match in re.finditer(_PREPOSITION, text[start:], re.IGNORECASE):
        begin = start + match.end()
        words: list[re.Match[str]] = []
        for word in _PLACE_WORD.finditer(text, begin):
            gap = text[(words[-1].end() if words else begin) : word.start()]
            # A comma doesn't end the place: "Portland, OR".
            if gap.strip() or word.group().rstrip(",").lower() in _NOT_PLACE:
                break
            words.append(word)
        if not words or len(words) > _MAX_PLACE_WORDS:
            continue
        end = words[-1].end() - (1 if words[-1].group().endswith(",") else 0)
        return PlaceSlot(text[begin:end], begin, end)
    return None


def place_terms(place: str) -> Place:
    """The patterns for a place name: a chosen ambiguous one ("Washington State") or the words."""
    words = _plain(place)
    for options in _AMBIGUOUS.values():
        for option in options:
            if words == _plain(option.label):
                return option
    name = " ".join(w.upper() if len(w) <= 2 else w.capitalize() for w in words.split())  # Washington DC
    return Place(name, f"%{words}%", f"%{words}%")


def is_ambiguous(place: str) -> bool:
    return _plain(place) in _AMBIGUOUS


def ask_which_place(question: str) -> str | None:
    """'Do you mean Washington DC or Washington State?' when the question names an ambiguous place."""
    slot = place_slot(question)
    if slot is None or not is_ambiguous(slot.place):
        return None
    return _which_prompt(_AMBIGUOUS[_plain(slot.place)])


def is_place_question(reply: str | None) -> bool:
    """Whether ``reply`` asked which of several places was meant."""
    return reply is not None and any(_which_prompt(options) in reply for options in _AMBIGUOUS.values())


def _which_prompt(options: Sequence[Place]) -> str:
    labels = [o.label for o in options]
    return f"Do you mean {', '.join(labels[:-1])} or {labels[-1]}?"


def resolve_place_answer(question: str, last_reply: str | None, answer: str) -> str | None:
    """``question`` with its ambiguous place replaced by the one ``answer`` picks, if it was asked."""
    slot = place_slot(question)
    if last_reply is None or slot is None or not is_ambiguous(slot.place):
        return None
    options = _AMBIGUOUS[_plain(slot.place)]
    if _which_prompt(options) not in last_reply:
        return None
    said = _plain(answer)
    picked = [o for o in options if _plain(o.label) in said]
    if not picked:
        words = set(said.split())
        picked = [
            o
            for o in options
            if any(
                (k in words) if len(k) > 2 else said in (k, f"{_plain(slot.place)} {k}") for k in o.keywords
            )
        ]
    if len(picked) != 1:
        return None
    return question[: slot.start] + picked[0].label + question[slot.end :]


def rewrite_place_follow_up(message: str, previous: str) -> str | None:
    """'how many in washington' after 'how many affiliates are in chicago?' -> the earlier
    question with the new place; None if it isn't such a follow-up."""
    match = _PLACE_FOLLOW_UP.match(message.strip())
    if match is None:
        return None
    new = place_slot("in " + match.group("place").strip())
    old = place_slot(previous)
    if new is None or old is None:
        return None
    return previous[: old.start] + new.place + previous[old.end :]


def standalone_questions(history: Sequence[LLMMessage]) -> list[str]:
    """Every user turn as a question that stands on its own, oldest first."""
    asked: list[str] = []
    last_reply: str | None = None
    for message in history:
        if message.role == "assistant":
            last_reply = message.content
            continue
        if message.role != "user":
            continue
        text = message.content.strip()
        if asked:
            text = (
                resolve_place_answer(asked[-1], last_reply, text)
                or rewrite_place_follow_up(text, asked[-1])
                or text
            )
        asked.append(text)
        last_reply = None
    return asked


def _plain(text: str) -> str:
    """Lower case words without dots and commas: 'Washington D.C.' -> 'washington dc'."""
    return " ".join(re.sub(r"[.,]", "", text.lower()).split())
