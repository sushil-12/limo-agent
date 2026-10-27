"""Understand a ride request typed in English, Hinglish or short form.

Rule based on purpose: the result decides what is booked, so it must be predictable and
testable. Anything not understood is simply left empty and the flow asks for it.

    "5 am pickup on 14 nov from ord to mdw"   -> 14 Nov, 05:00:00, ORD -> MDW
    "kal subah 5 baje ORD se MDW"              -> tomorrow, 05:00:00, ORD -> MDW
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
WEEKDAYS = {
    "monday": 0, "mon": 0, "somvar": 0, "tuesday": 1, "tue": 1, "tues": 1, "mangalvar": 1,
    "wednesday": 2, "wed": 2, "budhvar": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "guruvar": 3,
    "friday": 4, "fri": 4, "shukravar": 4, "saturday": 5, "sat": 5, "shanivar": 5,
    "sunday": 6, "sun": 6, "ravivar": 6, "itvaar": 6, "itwar": 6,
}  # fmt: skip
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5,
    "panch": 5, "chhe": 6, "che": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10, "zero": 0, "none": 0,
}  # fmt: skip
_NUM = r"(\d{1,3}|" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")"
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_ORD = r"(?:st|nd|rd|th)?"

_RE_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_RE_DAY_MONTH = re.compile(rf"\b(?:on\s+)?(\d{{1,2}}){_ORD}\s+(?:of\s+)?{_MONTH}(?:,?\s*(\d{{4}}))?", re.I)
_RE_MONTH_DAY = re.compile(rf"\b(?:on\s+)?{_MONTH}\s+(\d{{1,2}}){_ORD}(?:,?\s*(\d{{4}}))?\b", re.I)
_RE_RELATIVE = re.compile(
    r"\b(?:on\s+)?(day\s+after\s+tomorrow|parso[n]?|today|aaj|tonight|tomorrow|tmrw|tmr|kal)\b", re.I
)
_RE_WEEKDAY = re.compile(
    r"\b(?:on\s+|next\s+|this\s+|agle\s+)?(" + "|".join(WEEKDAYS) + r")\b(?:\s+ko)?", re.I
)

_PERIOD = r"(subah|savere|morning|dopahar|dupahar|afternoon|shaam|sham|evening|raat|night)"
_RE_AMPM = re.compile(r"\b(?:at\s+|@\s*)?(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])", re.I)
_RE_BAJE = re.compile(rf"\b(?:{_PERIOD}\s+)?(\d{{1,2}})(?:[:.](\d{{2}}))?\s*baje(?:\s+{_PERIOD})?\b", re.I)
_RE_24H = re.compile(
    r"\b(?:at\s+|@\s*)?([01]?\d|2[0-3]):([0-5]\d)(?:\s*(?:hrs|h|hours))?\b(?!\s*(?:am|pm))", re.I
)
_RE_AT_HOUR = re.compile(r"\b(?:at|@)\s*(\d{1,2})\b(?!\s*(?:pax|passengers?|people|bags?|hours?|hrs?))", re.I)
_RE_NOON = re.compile(r"\b(noon|midday|midnight)\b", re.I)
_RE_PERIOD_WORD = re.compile(rf"\b(?:in\s+the\s+)?{_PERIOD}\b", re.I)

_RE_ROUND = re.compile(
    r"\bround[\s-]?trip\b|\breturn\s+trip\b|\bwith\s+(?:a\s+)?return\b|\baana[\s-]+jaana\b|"
    r"\bup\s*(?:and|&)\s*down\b|\bwapas\s+bhi\b|\bboth\s+ways\b",
    re.I,
)
_RE_ONE_WAY = re.compile(r"\bone[\s-]?way\b|\bsirf\s+jaana\b|\bdrop\s+only\b", re.I)
_RE_CHARTER = re.compile(r"\bcharter(?:\s*/\s*tour)?\b|\bhourly\b|\bby\s+the\s+hour\b", re.I)
_RE_HOURS = re.compile(rf"\b(?:for\s+)?{_NUM}\s*(?:hours?|hrs?|ghante|ghanta)(?:\s+ke\s+liye)?\b", re.I)
_RE_PAX = re.compile(
    rf"\b{_NUM}\s*(?:pax|passengers?|people|persons?|adults?|guests?|travell?ers?|log(?:on)?|logo|members?)\b",
    re.I,
)
_RE_SOLO = re.compile(r"\b(?:just\s+me|only\s+me|myself\s+only|akela|akele|solo)\b", re.I)
_RE_BAGS = re.compile(
    rf"\b{_NUM}\s*(?:bags?|luggages?|suitcases?|baggages?|saman|pieces?\s+of\s+luggage)\b", re.I
)
_RE_NO_BAGS = re.compile(
    r"\b(?:no|without|zero)\s+(?:luggage|bags?|baggage)\b|\b(?:koi\s+)?saman\s+nahi\b|\bno\s+saman\b", re.I
)
_RE_NO_STOPS = re.compile(r"\bno\s+(?:extra\s+)?stops?\b|\bdirect\b|\bnon[\s-]?stop\b", re.I)

_FILLER_START = re.compile(
    r"^(?:(?:hi|hello|hey|please|pls|plz|kindly|can\s+you|could\s+you|i\s+(?:want|need|would\s+like)|"
    r"we\s+(?:want|need)|mujhe|hume|humein|book(?:ing)?|need|want|get|me|a|an|the|ride|car|cab|taxi|limo|"
    r"gaadi|gadi|transfer|trip|pickup|pick\s*up|pick-up|for|on|at|chahiye|karo|kar\s+do|ek)\b[\s,]*)+",
    re.I,
)
_FILLER_END = re.compile(
    r"(?:[\s,]*\b(?:please|pls|plz|tak|ko|jana|jaana|hai|chahiye|drop|karo|kar\s+do|on|at|for|and|"
    r"pickup|pick\s*up)\b)+[\s,.!?]*$",
    re.I,
)
_RE_FROM_TO = re.compile(r"\bfrom\s+(?P<a>.+?)\s+(?:to|till|until|->)\s+(?P<b>.+)$", re.I)
_RE_SE = re.compile(r"^(?P<a>.+?)\s+se\s+(?P<b>.+)$", re.I)
_RE_PICK_DROP = re.compile(
    r"\b(?:pick\s*up|pick-up|pickup)\s+(?:from|at|@)\s+(?P<a>.+?)"
    r"(?:\s+(?:and\s+)?(?:drop(?:\s*off)?|drop-off)\s+(?:at|to|@)\s+(?P<b>.+))?$",
    re.I,
)
_RE_FROM_ONLY = re.compile(r"\bfrom\s+(?P<a>.+)$", re.I)
_RE_DROP_ONLY = re.compile(r"\b(?:drop(?:\s*off)?|drop-off|going|go)\s+(?:at|to|@)\s+(?P<b>.+)$", re.I)
_RE_TO = re.compile(r"^(?P<a>.+?)\s+(?:to|->)\s+(?P<b>.+)$", re.I)

_YES = {
    "y", "yes", "yeah", "yep", "yup", "confirm", "confirmed", "ok", "okay", "sure", "haan", "han", "ha",
    "ji", "haan ji", "ji haan", "book", "book it", "go ahead", "proceed", "correct", "right", "theek hai",
}  # fmt: skip
_NO = {"n", "no", "nope", "nah", "nahi", "nahin", "na", "don't", "dont", "not now", "wrong", "skip"}


@dataclass
class TimeOfDay:
    hour: int
    minute: int = 0
    ambiguous: bool = False  # "at 5" / "5:30": morning or evening not said

    def value(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}:00"


@dataclass
class ParsedTrip:
    date: date | None = None
    relative_date: str | None = None  # the word used ("kal", "tomorrow"), to confirm the date
    time: TimeOfDay | None = None
    pickup: str | None = None
    dropoff: str | None = None
    service_type: str | None = None
    hours: int | None = None
    pax: int | None = None
    luggage: int | None = None
    no_stops: bool = False
    unparsed: list[str] = field(default_factory=list)


def parse_trip(text: str, today: date) -> ParsedTrip:
    parsed = ParsedTrip()
    rest = " " + " ".join(text.split()) + " "

    found_date, rest = _take_date(rest, today)
    if found_date is not None:
        parsed.date, parsed.relative_date = found_date
    parsed.time, rest = _take_time(rest)

    if _RE_ROUND.search(rest):
        parsed.service_type = "round_trip"
        rest = _RE_ROUND.sub(" ", rest)
    elif _RE_ONE_WAY.search(rest):
        parsed.service_type = "one_way"
        rest = _RE_ONE_WAY.sub(" ", rest)
    if hours := _RE_HOURS.search(rest):
        parsed.hours = _number(hours.group(1))
        parsed.service_type = parsed.service_type or "charter_tour"
        rest = rest[: hours.start()] + " " + rest[hours.end() :]
    if _RE_CHARTER.search(rest):
        parsed.service_type = "charter_tour"
        rest = _RE_CHARTER.sub(" ", rest)

    if pax := _RE_PAX.search(rest):
        parsed.pax = _number(pax.group(1))
        rest = rest[: pax.start()] + " " + rest[pax.end() :]
    elif _RE_SOLO.search(rest):
        parsed.pax = 1
        rest = _RE_SOLO.sub(" ", rest)
    if _RE_NO_BAGS.search(rest):
        parsed.luggage = 0
        rest = _RE_NO_BAGS.sub(" ", rest)
    elif bags := _RE_BAGS.search(rest):
        parsed.luggage = _number(bags.group(1))
        rest = rest[: bags.start()] + " " + rest[bags.end() :]
    if _RE_NO_STOPS.search(rest):
        parsed.no_stops = True
        rest = _RE_NO_STOPS.sub(" ", rest)

    parsed.pickup, parsed.dropoff = _places(" ".join(rest.replace(",", " , ").split()))
    return parsed


def parse_date(text: str, today: date) -> tuple[date, str | None] | None:
    found, _ = _take_date(f" {text} ", today)
    return found


def parse_time(text: str) -> TimeOfDay | None:
    found, _ = _take_time(f" {text} ")
    return found


def parse_count(text: str) -> int | None:
    cleaned = text.strip().lower()
    if cleaned in ("no", "none", "nahi", "nothing", "zero", "nil", "0", "no luggage", "no bags"):
        return 0
    match = re.search(_NUM, cleaned)
    if match is None:
        return None
    return _number(match.group(1))


def parse_yes_no(text: str) -> bool | None:
    cleaned = " ".join(re.sub(r"[^\w\s']", " ", text.lower()).split())
    if cleaned in _YES:
        return True
    if cleaned in _NO:
        return False
    return None


def parse_service(text: str) -> tuple[str, int | None] | None:
    """Answer to "one way, round trip or charter?" -> (service_type, hours)."""
    cleaned = text.strip().lower()
    hours = _RE_HOURS.search(cleaned)
    hour_count = _number(hours.group(1)) if hours else None
    if cleaned in ("1", "one way", "oneway", "one-way", "single") or _RE_ONE_WAY.search(cleaned):
        return "one_way", None
    if cleaned in ("2", "round", "return", "rt") or _RE_ROUND.search(cleaned):
        return "round_trip", None
    if cleaned in ("3", "hourly") or _RE_CHARTER.search(cleaned) or hours:
        return "charter_tour", hour_count
    return None


def _take_date(text: str, today: date) -> tuple[tuple[date, str | None] | None, str]:
    if match := _RE_ISO.search(text):
        found = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if found:
            return (found, None), _cut(text, match)
    for pattern, day_group, month_group in ((_RE_DAY_MONTH, 1, 2), (_RE_MONTH_DAY, 2, 1)):
        if match := pattern.search(text):
            day, month = int(match.group(day_group)), MONTHS[match.group(month_group)[:3].lower()]
            year = int(match.group(3)) if match.group(3) else None
            found = _with_year(day, month, year, today)
            if found:
                return (found, None), _cut(text, match)
    if match := _RE_RELATIVE.search(text):
        word = match.group(1).lower()
        offset = (
            2 if word.startswith(("day", "parso")) else 1 if word in ("tomorrow", "tmrw", "tmr", "kal") else 0
        )
        return (today + timedelta(days=offset), word), _cut(text, match)
    if match := _RE_WEEKDAY.search(text):
        weekday = WEEKDAYS[match.group(1).lower()]
        ahead = (weekday - today.weekday()) % 7 or 7
        return (today + timedelta(days=ahead), match.group(0).strip()), _cut(text, match)
    return None, text


def _take_time(text: str) -> tuple[TimeOfDay | None, str]:
    if match := _RE_AMPM.search(text):
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        pm = match.group(3).lower().startswith("p")
        if 1 <= hour <= 12 and minute < 60:
            hour = hour % 12 + (12 if pm else 0)
            return TimeOfDay(hour, minute), _cut(text, match)
    if match := _RE_BAJE.search(text):
        hour, minute = int(match.group(2)), int(match.group(3) or 0)
        period = (match.group(1) or match.group(4) or "").lower()
        rest = _cut(text, match)
        if not period and (word := _RE_PERIOD_WORD.search(rest)):
            period, rest = word.group(1).lower(), _cut(rest, word)
        if 0 <= hour <= 23 and minute < 60:
            return _with_period(hour, minute, period), rest
    if match := _RE_24H.search(text):
        hour, minute = int(match.group(1)), int(match.group(2))
        rest = _cut(text, match)
        if hour > 12 or hour == 0 or match.group(1).startswith("0"):
            return TimeOfDay(hour, minute), rest
        word = _RE_PERIOD_WORD.search(rest)
        if word:
            return _with_period(hour, minute, word.group(1).lower()), _cut(rest, word)
        return TimeOfDay(hour, minute, ambiguous=True), rest
    if match := _RE_NOON.search(text):
        return TimeOfDay(0 if match.group(1).lower() == "midnight" else 12), _cut(text, match)
    if match := _RE_AT_HOUR.search(text):
        hour = int(match.group(1))
        rest = _cut(text, match)
        if 1 <= hour <= 23:
            word = _RE_PERIOD_WORD.search(rest)
            if word:
                return _with_period(hour, 0, word.group(1).lower()), _cut(rest, word)
            return TimeOfDay(hour, 0, ambiguous=hour <= 12), rest
    return None, text


def _with_period(hour: int, minute: int, period: str) -> TimeOfDay:
    if hour > 12:
        return TimeOfDay(hour, minute)
    if period in ("subah", "savere", "morning"):
        return TimeOfDay(hour % 12, minute)
    if period in ("dopahar", "dupahar", "afternoon"):
        return TimeOfDay(hour if hour == 12 else hour + 12, minute)
    if period in ("shaam", "sham", "evening"):
        return TimeOfDay(hour % 12 + 12, minute)
    if period in ("raat", "night"):
        # raat 12 / 1-4 baje is after midnight; raat 7-11 is evening
        return (
            TimeOfDay(0 if hour == 12 else hour, minute)
            if hour == 12 or hour <= 4
            else TimeOfDay(hour + 12, minute)
        )
    return TimeOfDay(hour, minute, ambiguous=hour <= 12 and hour != 0)


def _places(text: str) -> tuple[str | None, str | None]:
    text = text.strip(" ,.!?")
    for pattern in (_RE_FROM_TO, _RE_PICK_DROP, _RE_SE):
        if match := pattern.search(text):
            return _clean(match.group("a")), _clean(match.groupdict().get("b"))
    if match := _RE_DROP_ONLY.search(text):
        head = _clean(text[: match.start()])
        if head and (frm := _RE_FROM_ONLY.search(head)):
            head = _clean(frm.group("a"))
        return head, _clean(match.group("b"))
    if match := _RE_FROM_ONLY.search(text):
        return _clean(match.group("a")), None
    cleaned = _clean(text)
    if cleaned and (match := _RE_TO.search(cleaned)):
        return _clean(match.group("a")), _clean(match.group("b"))
    return None, None


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    previous = None
    while previous != value:
        previous = value
        value = _FILLER_START.sub("", value.strip(" ,.!?"))
        value = _FILLER_END.sub("", value).strip(" ,.!?")
    return value or None


def _with_year(day: int, month: int, year: int | None, today: date) -> date | None:
    if year is not None:
        return _safe_date(year, month, day)
    found = _safe_date(today.year, month, day)
    if found is not None and found < today:
        found = _safe_date(today.year + 1, month, day)
    return found


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _number(value: str) -> int:
    return int(value) if value.isdigit() else NUMBER_WORDS[value.lower()]


def _cut(text: str, match: re.Match[str]) -> str:
    return text[: match.start()] + " " + text[match.end() :]
