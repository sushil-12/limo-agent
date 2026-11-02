"""The language model reads a ride request when the rules in ``parse`` miss part of it.

The rules run first and their answers stand. The model is only asked when the rules left the
pickup or drop-off empty or picked up words that are not a place ("mdw with and on 13dec"),
and only fills what the rules left empty. Everything it returns is checked against the user's
own words: a place must appear in what was typed, a count's number must have been typed, a date
or time needs a date or time word. Anything that fails a check is dropped and the flow asks for
it, so the model can make a question unnecessary but never invent a trip. The summary and the
explicit "yes" before booking stay as they are.

A slow or failing model is logged and the rules' answer is used.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import date, timedelta
from typing import Any

import orjson

from app.core.logging import get_logger
from app.llm.base import LLMError, LLMMessage, LLMProvider
from app.services.booking.parse import NUMBER_WORDS, ParsedTrip, TimeOfDay, _clean

logger = get_logger(__name__)

_MAX_TOKENS = 200
_MAX_PLACE = 120
_MAX_COUNT = 60
_SERVICES = ("one_way", "round_trip", "charter_tour")

_PROMPT = """You extract a limousine ride request into JSON. Today is {today} ({weekday}).

Return ONLY one JSON object with these keys (null when the user did not say it):
  "pickup":   pickup place exactly as the user wrote it (address, airport code, landmark)
  "dropoff":  drop-off place exactly as the user wrote it
  "date":     pickup date as YYYY-MM-DD (the next such date if no year is given)
  "time":     pickup time as HH:MM, 24-hour
  "service":  "one_way", "round_trip" or "charter_tour" (hourly)
  "hours":    charter hours, a number
  "pax":      number of passengers
  "luggage":  number of bags / suitcases / luggage pieces

Rules: copy places word for word from the user's message, never shorten, expand or guess them,
and never take one from the example. A place is where the car goes: "lands at midway" means
the pickup is "midway". Do not put counts, dates or times inside a place. Never invent a value.

Example message: "need a car from Union Station to 12 Elm St for 3 people with 2 bags on 5dec at 9am"
{{"pickup": "Union Station", "dropoff": "12 Elm St", "date": "{example_date}", "time": "09:00",
"service": null, "hours": null, "pax": 3, "luggage": 2}}"""

# Never part of a place: counts, dates and times. The model's places may not hold these.
_NOT_A_PLACE = re.compile(
    r"\b(?:passengers?|pax|people|persons?|bags?|luggages?|suitcases?|baggages?|"
    r"a\.?m\.?|p\.?m\.?|tomorrow|today|tonight|morning|afternoon|evening|hours?|hrs?)\b|"
    r"\b\d{1,2}\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|"
    r"\b(?:on|at)\s+\d|\b\d{1,2}:\d{2}\b",
    re.IGNORECASE,
)
# Rarely part of a place, often left behind when the rules cut a sentence apart badly
# ("ord terminal 3 and take us downtown"). Seeing one asks the model; it does not drop the place.
_SPEECH = re.compile(r"\b(?:with|and|take|taking|us|me|my|our|going|then|please|need|want)\b", re.IGNORECASE)
_DATE_WORD = re.compile(
    r"\d|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|\b(?:today|tonight|tomorrow|tmrw|"
    r"tmr|kal|parso|aaj|mon|tue|wed|thu|fri|sat|sun)[a-z]*\b",
    re.IGNORECASE,
)
_TIME_WORD = re.compile(r"\d|\b(?:noon|midday|midnight)\b", re.IGNORECASE)
# Said whether it is morning or evening, so an hour of 1-12 is not ambiguous.
_DAY_HALF = re.compile(
    r"\d\s*(?:a\.?m\.?|p\.?m\.?)(?![a-z])|\b(?:am|pm|morning|afternoon|evening|night|tonight|noon|"
    r"midnight|subah|savere|dopahar|dupahar|shaam|sham|raat)\b",
    re.IGNORECASE,
)
_NO_BAGS = re.compile(r"\b(?:no|without|zero)\s+(?:luggage|bags?|baggage|suitcases?)\b", re.IGNORECASE)
_SERVICE_WORDS = {
    "round_trip": re.compile(r"\bround|\breturn\b|\bboth\s+ways\b|\bwapas\b|\baana", re.IGNORECASE),
    "one_way": re.compile(r"\bone[\s-]?way\b|\bdrop\s+only\b|\bsirf\s+jaana\b", re.IGNORECASE),
    "charter_tour": re.compile(r"\bcharter|\bhourly\b|\bhours?\b|\bhrs?\b|\bghant|\btour\b", re.IGNORECASE),
}


def needs_reading(text: str, parsed: ParsedTrip) -> bool:
    """Whether the rules left something the message seems to say."""
    if _clean(" ".join(text.split())) is None:  # only "book a ride", "help me create a booking"
        return False
    places = (parsed.pickup, parsed.dropoff)
    return any(p is None or looks_wrong(p) or _SPEECH.search(p) is not None for p in places)


def looks_wrong(place: str) -> bool:
    """A place that holds counts, dates or times, so cannot be right as it is."""
    return len(place) > _MAX_PLACE or _NOT_A_PLACE.search(place) is not None


class TripReader:
    """Asks the model for the parts of a ride request the rules could not read."""

    def __init__(self, provider: LLMProvider, *, timeout_seconds: float = 8.0) -> None:
        self._provider = provider
        self._timeout = timeout_seconds

    async def improve(self, text: str, parsed: ParsedTrip, today: date) -> ParsedTrip:
        """``parsed`` with the model's checked answers filling its gaps. Never raises."""
        if not needs_reading(text, parsed):
            return parsed
        started = time.monotonic()
        try:
            raw = await asyncio.wait_for(self._ask(text, today), self._timeout)
        except (LLMError, TimeoutError) as exc:
            logger.warning("booking_read_failed", error=type(exc).__name__, ms=_ms_since(started))
            return parsed
        found = read_answer(raw, text, today)
        merged = combine(parsed, found)
        logger.info(
            "booking_read",
            ms=_ms_since(started),
            filled=sorted(k for k, v in vars(found).items() if v not in (None, False, [])),
        )
        return merged

    async def _ask(self, text: str, today: date) -> str:
        example = today + timedelta(days=1)
        prompt = _PROMPT.format(
            today=today.isoformat(), weekday=today.strftime("%A"), example_date=example.isoformat()
        )
        result = await self._provider.chat(
            [LLMMessage("system", prompt), LLMMessage("user", text)],
            max_tokens=_MAX_TOKENS,
            temperature=0.0,
        )
        return result.content


def read_answer(raw: str, text: str, today: date) -> ParsedTrip:
    """The model's JSON as a ParsedTrip, keeping only values the user's words back up."""
    found = ParsedTrip()
    data = _json_object(raw)
    if data is None:
        return found
    found.pickup = _place(data.get("pickup"), text)
    found.dropoff = _place(data.get("dropoff"), text)
    if found.pickup is not None and found.pickup == found.dropoff:
        found.dropoff = None
    if _DATE_WORD.search(text):
        found.date = _date(data.get("date"), today)
    if _TIME_WORD.search(text):
        found.time = _time(data.get("time"), text, said_half=_DAY_HALF.search(text) is not None)
    found.pax = _count(data.get("pax"), text, low=1)
    found.luggage = 0 if _NO_BAGS.search(text) else _count(data.get("luggage"), text, low=0)
    service = data.get("service")
    if service in _SERVICES and _SERVICE_WORDS[service].search(text):
        found.service_type = service
        if service == "charter_tour":
            found.hours = _count(data.get("hours"), text, low=1, high=24)
    return found


def combine(rules: ParsedTrip, found: ParsedTrip) -> ParsedTrip:
    """The rules' answer, with the model's where the rules have none. The model's place also
    replaces one that cannot be right, or one it is a trimmed part of ("ord terminal 3" in
    "ord terminal 3 and take us downtown")."""
    merged = ParsedTrip(**vars(rules))
    for name in ("date", "time", "service_type", "hours", "pax", "luggage"):
        if getattr(merged, name) is None and getattr(found, name) is not None:
            setattr(merged, name, getattr(found, name))
    if merged.date is not None and rules.date is None:
        merged.relative_date = None
    for name in ("pickup", "dropoff"):
        mine, theirs = getattr(rules, name), getattr(found, name)
        if theirs is not None and (
            mine is None or looks_wrong(mine) or _trims(theirs, mine) or _chattier(mine, theirs)
        ):
            setattr(merged, name, theirs)
        elif theirs is None and mine is not None and looks_wrong(mine):
            setattr(merged, name, None)  # the flow asks for it instead of searching a sentence
    return merged


def _trims(theirs: str, mine: str) -> bool:
    short, long = _words(theirs), _words(mine)
    return short != long and f" {short} " in f" {long} "


def _chattier(mine: str, theirs: str) -> bool:
    """ "hilton with my wife" against "the hilton"."""
    return _SPEECH.search(mine) is not None and _SPEECH.search(theirs) is None


def _json_object(raw: str) -> dict[str, Any] | None:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = orjson.loads(raw[start : end + 1])
    except orjson.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _words(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.lower()).split())


def _place(value: Any, text: str) -> str | None:
    """A place the user typed: the model may only copy it, so it must be in the message."""
    if not isinstance(value, str):
        return None
    value = " ".join(value.split()).strip(" ,.!?")
    if not value or len(value) > _MAX_PLACE or looks_wrong(value):
        return None
    words = _words(value)
    return value if words and f" {words} " in f" {_words(text)} " else None


def _date(value: Any, today: date) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        found = date.fromisoformat(value.strip())
    except ValueError:
        return None
    return found if today <= found <= today + timedelta(days=400) else None


def _time(value: Any, text: str, *, said_half: bool) -> TimeOfDay | None:
    """A time whose hour the user typed ("9", "09", "21", "noon"): a lone menu number such as
    "1." must not let the model hand back the 09:00 from its example."""
    if not isinstance(value, str) or (match := re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)) is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59 or not _hour_typed(hour, text):
        return None
    if said_half or hour == 0 or hour > 12:  # "21:30" says evening by itself
        return TimeOfDay(hour, minute)
    # Morning or evening was not said: the flow asks am/pm (as it does for "at 5").
    return TimeOfDay(hour % 12 or 12, minute, ambiguous=True)


def _hour_typed(hour: int, text: str) -> bool:
    if hour == 12 and re.search(r"\b(?:noon|midday)\b", text, re.IGNORECASE):
        return True
    if hour == 0 and re.search(r"\bmidnight\b", text, re.IGNORECASE):
        return True
    return any(re.search(rf"(?<!\d)0?{h}(?!\d)", text) for h in {hour, hour % 12 or 12})


def _count(value: Any, text: str, *, low: int, high: int = _MAX_COUNT) -> int | None:
    """A count whose number the user typed, as digits or a word ("two")."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = int(float(value))
    except ValueError:
        return None
    if not low <= number <= high:
        return None
    typed = _words(text).split()
    words = {w for w, n in NUMBER_WORDS.items() if n == number}
    return number if str(number) in typed or words & set(typed) else None


def _ms_since(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)
