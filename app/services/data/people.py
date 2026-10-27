# ruff: noqa: S608 - SQL is built from fixed text, int ids and name words reduced by _clean()
# to letters, digits, dots and hyphens, then checked by sql_guard like every other query.
"""Questions about a person by name: resolve the name, pick the person, then query.

"Upcoming bookings for Kevin Fleming" can't be answered by putting the name into a
booking query, and one name often belongs to several people (Joseph Anselmo is an
admin, an affiliate, a chauffeur and three customers). So, as PERSON_LOOKUP.md lays out:

1. Find every account, chauffeur, affiliate company, travel agency and lose affiliate with
   that name. An affiliate and its own driver with the same name are one person.
2. Drop those without bookings; if a role was named ("driver Joseph"), keep only it.
   One left: use it. Several: ask which one, and take the answer on the next turn.
3. Query bookings through the column that links that kind of person to a booking.

The "which one?" menu stays open until another menu replaces it: right after it any reply
is an answer, later only a bare number ("3", "option 3"). Result lists are not menus, so
they show booking numbers (``Booking #1965``) rather than 1, 2, 3.

All SQL here is fixed text plus sanitised name words; it still runs through
``sql_guard``. Admin only: other roles can't search people outside their scope.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from app.llm.base import LLMMessage
from app.services.data.catalog import CLOSED_STATUSES
from app.services.data.runner import QueryRows

PICK_PROMPT = "Which one? Reply with the number."

Kind = Literal["customer", "travel_agent", "affiliate", "driver", "lose_affiliate", "account"]
When = Literal["upcoming", "past", "all"]
Run = Callable[[str], Awaitable[QueryRows]]

_NAME = r"(?P<name>[a-z][a-z.'-]*(?:\s+[a-z][a-z.'-]*){0,3})"
_ROLES = "customer|client|passenger|rider|driver|chauffeur|affiliate|operator|travel agent|agent"
_ROLE = rf"(?:(?P<role>{_ROLES})\s+)?"
_BOOKINGS = r"\b(?:bookings?|rides?|trips?|reservations?)\b"
_FOR_PERSON = re.compile(
    _BOOKINGS + r".*?\b(?:for|of|by|with)\s+" + _ROLE + _NAME + r"\s*[?.!]*\s*$", re.IGNORECASE
)
_PERSONS = re.compile(
    r"^\W*(?:.*?\b(?:of|for)\s+)?" + _ROLE + _NAME + r"'s\s+(?:\w+\s+){0,2}?" + _BOOKINGS, re.IGNORECASE
)
# Words that make "bookings for <x>" about something other than a person.
_NOT_A_NAME = frozenset(
    [
        "today",
        "tomorrow",
        "yesterday",
        "next",
        "this",
        "last",
        "week",
        "weeks",
        "month",
        "months",
        "year",
        "years",
        "upcoming",
        "past",
        "future",
        "me",
        "my",
        "us",
        "our",
        "him",
        "her",
        "them",
        "all",
        "every",
        "each",
        "any",
        "the",
        "a",
        "an",
        "booking",
        "bookings",
        "ride",
        "rides",
        "trip",
        "trips",
        "airport",
        "city",
        "pickup",
        "dropoff",
        "date",
        "day",
        "days",
        "time",
        "hour",
        "hours",
        "now",
        "affiliate",
        "affiliates",
        "lose",
        "loose",
        "driver",
        "drivers",
        "customer",
        "customers",
        "agent",
        "agents",
        "status",
        "pending",
        "cancelled",
        "paid",
        "unpaid",
        "unassigned",
        "assigned",
        "no",
        "without",
        "sedan",
        "suv",
        "van",
        "limo",
        "vehicle",
        "vehicles",
        "currency",
        "usd",
        "it",
        "that",
        "those",
        "these",
    ]
)
_ROLE_KIND: dict[str, Kind] = {
    "customer": "customer",
    "client": "customer",
    "passenger": "customer",
    "rider": "customer",
    "driver": "driver",
    "chauffeur": "driver",
    "affiliate": "affiliate",
    "operator": "affiliate",
    "agent": "travel_agent",
    "travel agent": "travel_agent",
}
_UPCOMING = re.compile(r"\b(upcoming|next|future|scheduled|coming)\b", re.IGNORECASE)
_PAST = re.compile(r"\b(past|previous|completed|old|earlier|history)\b", re.IGNORECASE)
_COUNT = re.compile(r"\b(how many|count|number of)\b", re.IGNORECASE)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7}
# "3", "#3", "option 3", "no. 3", "number 3", "the third one"
_BARE_CHOICE = re.compile(
    r"^\W*(?:(?:option|opt|no|number|num|choice)\.?\s*)?#?\s*(?:\d{1,2}|(?:the\s+)?(?:"
    + "|".join(_ORDINALS)
    + r")(?:\s+one)?)\W*$",
    re.IGNORECASE,
)
_MENU_LINE = re.compile(r"^(?P<number>\d+)\. (?P<who>.*?) — \d+ bookings?$", re.MULTILINE)

# users.role -> how that account relates to bookings.
_ACCOUNT_KIND: dict[int, Kind] = {
    2: "affiliate",
    9: "affiliate",
    3: "travel_agent",
    7: "travel_agent",
    5: "customer",
}
_KIND_LABEL: dict[Kind, str] = {
    "customer": "Customer",
    "travel_agent": "Travel agent",
    "affiliate": "Affiliate",
    "driver": "Driver",
    "lose_affiliate": "Lose affiliate",
    "account": "Account",
}
_BOOKING_FILTER: dict[Kind, str] = {
    # A customer books themselves or is booked by a travel agent.
    "customer": "(r.acc_id IN ({ids}) OR r.travel_client_id IN ({ids}))",
    "account": "(r.acc_id IN ({ids}) OR r.travel_client_id IN ({ids}))",
    "travel_agent": "r.acc_id IN ({ids})",
    "affiliate": "r.affiliate_id IN ({ids})",
    "driver": "r.driver_id IN ({ids})",
    "lose_affiliate": "r.lose_affiliate IN ({ids})",
}


@dataclass(frozen=True, slots=True)
class PersonQuestion:
    name: str
    words: tuple[str, ...]
    kind: Kind | None  # the role the user named, if any
    when: When
    count: bool


@dataclass(frozen=True, slots=True)
class Candidate:
    kind: Kind
    ids: tuple[int, ...]  # lose affiliates get a new row per booking, so several
    name: str
    detail: str  # email, company or operator: what tells same-named people apart
    bookings: int
    driver_ids: tuple[int, ...] = ()  # an affiliate who also drives for their own company


@dataclass(frozen=True, slots=True)
class PersonAnswer:
    """A reply to show as is, plus the booking query and its rows when there are any."""

    message: str
    sql: str | None = None
    rows: QueryRows | None = None


def parse_person_question(question: str) -> PersonQuestion | None:
    """The person and what is asked about them, or None if no person is named."""
    question = question.strip()
    match = _FOR_PERSON.search(question) or _PERSONS.search(question)
    if match is None:
        return None
    name = " ".join(match.group("name").split())
    words = tuple(w for w in (_clean(w) for w in name.split()) if w)
    if not words or any(w in _NOT_A_NAME for w in name.lower().split()):
        return None
    role = (match.group("role") or "").lower()
    when: When = "upcoming" if _UPCOMING.search(question) else "past" if _PAST.search(question) else "all"
    return PersonQuestion(name.title(), words, _ROLE_KIND.get(role), when, bool(_COUNT.search(question)))


@dataclass(frozen=True, slots=True)
class MenuReply[Q]:
    """A reply choosing from an open "which one?" menu."""

    question: Q  # what the menu asked about
    choice: str
    clash: str | None = None  # set when the number could also mean a booking in the latest list


def is_bare_choice(text: str) -> bool:
    """'3', 'option 3', 'no. 3', 'the third one': a pick from a menu, nothing else."""
    return _BARE_CHOICE.match(text.strip()) is not None


def answers_menu(history: Sequence[LLMMessage]) -> bool:
    """Whether the last user message answers an open "which one?" menu."""
    return _open_menu(history, lambda question: question) is not None


def answering_menu[Q](history: Sequence[LLMMessage], parse: Callable[[str], Q | None]) -> MenuReply[Q] | None:
    """The question an open menu asked about (as ``parse`` reads it) and the reply choosing from it."""
    found = _open_menu(history, parse)
    if found is None:
        return None
    question, menu, latest = found
    choice = history[-1].content
    clash = None if latest else _clash(menu, history[-2].content, choice)
    return MenuReply(question, choice, clash)


def _open_menu[Q](
    history: Sequence[LLMMessage], parse: Callable[[str], Q | None]
) -> tuple[Q, str, bool] | None:
    """The latest menu, the question it asked about, and whether it is the last reply.

    Right after the menu any reply answers it; once other replies follow, only a bare
    number does, until a newer menu replaces it.
    """
    if not history or history[-1].role != "user":
        return None
    last = len(history) - 1
    for index in range(last - 1, -1, -1):
        menu = history[index]
        if menu.role != "assistant" or PICK_PROMPT not in menu.content:
            continue
        latest = index == last - 1
        if not latest and not is_bare_choice(history[-1].content):
            return None
        # The question before the menu. A reply the menu didn't understand got the menu
        # again, so step back over replies that themselves follow a menu.
        at = index - 1
        while at >= 0:
            if history[at].role == "user":
                if (question := parse(history[at].content)) is not None:
                    return question, menu.content, latest
                if at == 0 or PICK_PROMPT not in history[at - 1].content:
                    return None
            at -= 1
        return None
    return None


def _clash(menu: str, latest_reply: str, choice: str) -> str | None:
    """'Do you mean option 3 (...) or booking #3?' when both are on screen."""
    number = re.search(r"\d+", choice)
    if number is None or not re.search(rf"\bBooking #{number.group()}\b", latest_reply):
        return None
    option = next((m for m in _MENU_LINE.finditer(menu) if m.group("number") == number.group()), None)
    if option is None:
        return None
    return f"Do you mean option {number.group()} ({option.group('who')}) or booking #{number.group()}?"


async def answer_person_question(asked: PersonQuestion, run: Run, choice: str | None = None) -> PersonAnswer:
    """Resolve the person, then look up their bookings. ``choice`` answers "which one?"."""
    candidates = await find_people(asked.words, asked.kind, run)
    if not candidates:
        return PersonAnswer(f"I couldn't find anyone named {asked.name}.")
    if len(candidates) > 1:
        picked = pick_person(choice, candidates) if choice is not None else None
        if picked is None:
            return PersonAnswer(ask_which(asked.name, candidates))
        return await _bookings(asked, picked, run)
    return await _bookings(asked, candidates[0], run)


async def find_people(words: Sequence[str], kind: Kind | None, run: Run) -> list[Candidate]:
    """People with these name words (of ``kind``, if given), those with bookings first; with
    any bookings at all, the ones without are dropped."""
    candidates = await _candidates(words, run)
    if kind is not None:
        candidates = [c for c in candidates if c.kind == kind]
    return [c for c in candidates if c.bookings > 0] or candidates


async def _candidates(words: Sequence[str], run: Run) -> list[Candidate]:
    person = " AND ".join(
        f"CONCAT_WS(' ', {{t}}.first_name, {{t}}.middle_name, {{t}}.last_name) LIKE '%{w}%'" for w in words
    )
    company = " AND ".join(
        f"COALESCE(NULLIF(ac.company_name, ''), tp.agency_name) LIKE '%{w}%'" for w in words
    )
    accounts = await run(
        "SELECT a.id, u.role, CONCAT_WS(' ', a.first_name, a.last_name) AS name, a.email, "
        "COALESCE(NULLIF(ac.company_name, ''), tp.agency_name) AS company, "
        "(SELECT COUNT(*) FROM tbl_reservations r WHERE r.acc_id = a.id) AS as_booker, "
        "(SELECT COUNT(*) FROM tbl_reservations r WHERE r.travel_client_id = a.id) AS as_passenger, "
        "(SELECT COUNT(*) FROM tbl_reservations r WHERE r.affiliate_id = a.id) AS as_affiliate "
        "FROM tbl_account a LEFT JOIN users u ON u.id = a.user_id "
        "LEFT JOIN tbl_affiliate_company ac ON ac.acc_id = a.id "
        "LEFT JOIN tbl_travel_planner tp ON tp.acc_id = a.id "
        f"WHERE COALESCE(a.soft_delete, 0) = 0 AND ({person.format(t='a')} OR {company}) "
        "ORDER BY a.id LIMIT 20"
    )
    drivers = await run(
        "SELECT d.id, d.acc_id, CONCAT_WS(' ', d.first_name, d.last_name) AS name, ac.company_name, "
        "(SELECT COUNT(*) FROM tbl_reservations r WHERE r.driver_id = d.id) AS trips "
        "FROM tbl_driver_setting d LEFT JOIN tbl_affiliate_company ac ON ac.acc_id = d.acc_id "
        f"WHERE {person.format(t='d')} ORDER BY d.id LIMIT 20"
    )
    # Bookings point at a lose affiliate through tbl_reservations.lose_affiliate (its reservation_id
    # is only the booking it was first entered for), so that is what is counted.
    lose = await run(
        "SELECT GROUP_CONCAT(l.id) AS ids, MAX(l.name) AS name, MAX(l.operator_name) AS operator_name, "
        "SUM((SELECT COUNT(*) FROM tbl_reservations r WHERE r.lose_affiliate = l.id)) AS trips "
        "FROM tbl_lose_affiliates l WHERE COALESCE(l.soft_delete, 0) = 0 AND ("
        + " AND ".join(f"CONCAT_WS(' ', l.name, l.operator_name) LIKE '%{w}%'" for w in words)
        + ") GROUP BY LOWER(TRIM(l.name)), LOWER(TRIM(l.operator_name)) LIMIT 20"
    )

    found: list[Candidate] = []
    for acc_id, role, name, email, company, as_booker, as_passenger, as_affiliate in accounts.rows:
        kind = _ACCOUNT_KIND.get(int(role or 0), "account")
        if kind == "affiliate":
            found.append(Candidate(kind, (acc_id,), name, company or email or "", int(as_affiliate or 0)))
        elif kind == "travel_agent":
            found.append(Candidate(kind, (acc_id,), name, company or email or "", int(as_booker or 0)))
        else:
            bookings = int(as_booker or 0) + int(as_passenger or 0)
            found.append(Candidate(kind, (acc_id,), name, email or "", bookings))
    affiliates = {c.ids[0]: i for i, c in enumerate(found) if c.kind == "affiliate"}
    for driver_id, acc_id, name, company, trips in drivers.rows:
        owner = affiliates.get(acc_id)
        if owner is not None and _same_name(found[owner].name, name):
            # The affiliate drives for their own company: one person, whose driver trips are
            # bookings of that company too.
            merged = found[owner]
            found[owner] = Candidate(
                merged.kind,
                merged.ids,
                merged.name,
                merged.detail,
                max(merged.bookings, int(trips or 0)),
                (*merged.driver_ids, driver_id),
            )
            continue
        found.append(Candidate("driver", (driver_id,), name, company or "", int(trips or 0)))
    for ids, name, operator_name, trips in lose.rows:
        id_list = tuple(int(i) for i in str(ids).split(",") if i.strip().isdigit())
        found.append(
            Candidate(
                "lose_affiliate", id_list, name or operator_name or "", operator_name or "", int(trips or 0)
            )
        )
    return sorted(found, key=lambda c: -c.bookings)


def booking_filter(person: Candidate) -> str:
    """The condition on ``tbl_reservations r`` for this person's bookings."""
    condition = _BOOKING_FILTER[person.kind].format(ids=", ".join(str(int(i)) for i in person.ids))
    if person.driver_ids:
        condition = f"({condition} OR r.driver_id IN ({', '.join(str(int(i)) for i in person.driver_ids)}))"
    return condition


async def _bookings(asked: PersonQuestion, person: Candidate, run: Run) -> PersonAnswer:
    where = f"r.status = 'enable' AND {booking_filter(person)}"
    if asked.when == "upcoming":
        where += f" AND r.pickup_date >= CURDATE() AND r.booking_status NOT IN ({CLOSED_STATUSES})"
        order, label = "r.pickup_date, r.pickup_time", "upcoming bookings"
    elif asked.when == "past":
        where += " AND r.pickup_date < CURDATE()"
        order, label = "r.pickup_date DESC, r.pickup_time DESC", "past bookings"
    else:
        order, label = "r.pickup_date DESC, r.pickup_time DESC", "bookings"
    who = f"{person.name} ({describe(person)})"
    total_rows = await run(f"SELECT COUNT(*) AS total FROM tbl_reservations r WHERE {where}")
    total = int(total_rows.rows[0][0]) if total_rows.rows else 0
    if asked.count or total == 0:
        return PersonAnswer(f"{who} has {total} {label}.")
    sql = (
        "SELECT r.id, r.pickup_date, r.pickup_time, "
        "COALESCE(NULLIF(r.pickup, ''), r.pickup_address) AS pickup, "
        "COALESCE(NULLIF(r.dropoff, ''), r.dropoff_address) AS dropoff, r.booking_status "
        f"FROM tbl_reservations r WHERE {where} ORDER BY {order} LIMIT 20"
    )
    return PersonAnswer(f"{who} has {total} {label}:", sql, await run(sql))


def ask_which(name: str, candidates: Sequence[Candidate]) -> str:
    """The "which one?" menu: every option with its name, role and number of bookings."""
    lines = [f"I found several people named {name}:"]
    lines += [
        f"{number}. {c.name or 'No name'} — {describe(c)} — {_count(c.bookings)}"
        for number, c in enumerate(candidates, start=1)
    ]
    return "\n".join([*lines, PICK_PROMPT])


def describe(person: Candidate) -> str:
    """What the person is: 'Affiliate #847 (Dg Limo Service LLC) & driver', 'Customer #27, x@y.com'."""
    label = _KIND_LABEL[person.kind]
    if person.kind == "driver":
        return f"{label} for {person.detail}" if person.detail else label
    if person.kind == "lose_affiliate":
        same = person.detail.casefold() == person.name.casefold()
        return label + (f", operator {person.detail}" if person.detail and not same else "")
    if person.kind == "affiliate":
        company = f" ({person.detail})" if person.detail else ""
        return f"{label} #{person.ids[0]}{company}" + (" & driver" if person.driver_ids else "")
    return f"{label} #{person.ids[0]}" + (f", {person.detail}" if person.detail else "")


def pick_person(reply: str, candidates: Sequence[Candidate]) -> Candidate | None:
    """The candidate a reply to "which one?" points at: its number, id, email or role."""
    text = reply.strip().lower()
    for word, number in _ORDINALS.items():
        if re.search(rf"\b{word}\b", text):
            return candidates[number - 1] if number <= len(candidates) else None
    for number in (int(n) for n in re.findall(r"\d+", text)):
        if 1 <= number <= len(candidates) and len(text) <= 12:  # "2", "#2", "number 2"
            return candidates[number - 1]
        by_id = [c for c in candidates if number in c.ids and c.kind not in ("driver", "lose_affiliate")]
        if len(by_id) == 1:
            return by_id[0]
    by_detail = [c for c in candidates if c.detail and c.detail.lower() in text]
    if len(by_detail) == 1:
        return by_detail[0]
    by_role = [c for c in candidates if any(role in text for role in _roles(c))]
    return by_role[0] if len(by_role) == 1 else None


def _roles(person: Candidate) -> tuple[str, ...]:
    label = _KIND_LABEL[person.kind].lower()
    return (label, "driver") if person.driver_ids else (label,)


def _count(bookings: int) -> str:
    return f"{bookings} booking{'' if bookings == 1 else 's'}"


def _same_name(a: str | None, b: str | None) -> bool:
    return " ".join((a or "").casefold().split()) == " ".join((b or "").casefold().split())


def _clean(word: str) -> str:
    """A name word that is safe inside '...': letters, digits, dots, hyphens; ' matches any char."""
    return re.sub(r"[^a-z0-9.\-_]", "", word.lower().replace("'", "_"))
