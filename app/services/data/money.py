# ruff: noqa: S608 - SQL is built from fixed text, int ids and name words reduced by people._clean(),
# then checked by sql_guard like every other query.
"""Money questions answered with fixed SQL: a booking's prices, and profit / income / spend totals.

The model got these wrong in ways that look right: it looked up prices by order id
(``tbl_order.id`` is not the booking number, so it showed another customer's prices),
invented a tax rate, and added filters nobody asked for. So:

- A booking's rate / breakdown / taxes / grand total always go through
  ``tbl_order.reservation_id = <booking>``, for that booking whatever its status.
- Totals only read and sum stored values (``share_array``, ``tbl_order.grand_total``),
  never compute a share, per currency, without cancelled/refunded bookings, and with only
  the filters asked for (a party and/or a period).
- What "profit" means depends on who asks: an admin gets the admin share with the whole
  split, an affiliate their affiliate share, a travel agent their travel agent share, and a
  customer what they spent. Scoped users only ever see their own bookings (``sql_guard``).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.services.data.people import (
    Kind,
    ask_which,
    booking_filter,
    describe,
    find_people,
    pick_person,
)
from app.services.data.runner import QueryRows
from app.services.prompts import BOOKING_STATUS_LABELS

Asker = Literal["admin", "affiliate", "travel_agent", "customer"]
Run = Callable[[str], Awaitable[QueryRows]]

STRIPE_REPLY = "Stripe status information isn't available in the chatbot yet."
_STRIPE = re.compile(r"\bstripe\b", re.IGNORECASE)

_ASKERS: dict[str, Asker] = {
    "admin": "admin",
    "sub_admin": "admin",
    "driver": "affiliate",
    "affiliate_driver": "affiliate",
    "travel_agent": "travel_agent",
    "rider": "customer",
}
_EXCLUDED = "r.booking_status NOT IN ('cancelled', 'rejected', 'refund')"
_HAS_SHARES = "r.share_array IS NOT NULL AND r.share_array LIKE '{%'"
# A named booking is answered whatever its status; these are mentioned with the answer.
_NOT_ACTIVE = frozenset(
    {"cancelled", "cancellation_requested", "rejected", "rejection_requested", "refund", "partial_refund"}
)

# "booking 2109", "booking #2109", "booking no. 2109", "#2109", "2109 booking"
_BOOKING_NUMBER = re.compile(
    r"\b(?:booking|reservation|ride|trip)s?\s*(?:number|no\.?|id)?\s*[:#]?\s*(?P<after>\d{2,7})\b|"
    r"#(?P<hash>\d{2,7})\b|\b(?P<before>\d{2,7})(?:'s)?\s+(?:booking|reservation|ride|trip)\b",
    re.IGNORECASE,
)
_TAXES = re.compile(r"\btax(?:es)?\b", re.IGNORECASE)
_BREAKDOWN = re.compile(
    r"\b(?:rates?|break ?down|charges|itemi[sz]ed|line items?|price details?|fare details?)\b", re.IGNORECASE
)
_TOTAL = re.compile(r"\b(?:grand total|total|price|cost|amount|fare|how much)\b", re.IGNORECASE)
# A booking's shares, pre-charges or payments are other questions (other columns).
_NOT_THE_TOTAL = re.compile(
    r"\b(?:shares?|profit|commission|earn\w*|income|payouts?|admin|affiliate|agent|driver|charged|pre-?charged|"
    r"refund\w*|deposit)\b",
    re.IGNORECASE,
)
# A follow-up with these is about more than the booking asked about before.
_ELSEWHERE = re.compile(
    r"\b(?:my|our|all|every|each|this|last|today|month|year|week|how many|count|bookings|rides|trips)\b",
    re.IGNORECASE,
)
# Rankings and breakdowns ("top drivers by earnings") are not one total.
_RANKING = re.compile(
    r"\b(?:top|best|most|highest|lowest|least|per|each|every|rank\w*|average|avg|compare\w*|breakdown|list)\b",
    re.IGNORECASE,
)
_FOLLOW_UP_WORDS = 8

_TOTALS_WORDS: dict[Asker, re.Pattern[str]] = {
    "admin": re.compile(
        r"\b(?:profits?|earn(?:ed|ing|ings|s)?|revenue|income|margins?|commissions?)\b", re.I
    ),
    "affiliate": re.compile(
        r"\b(?:profits?|income|earn(?:ed|ing|ings|s)?|payouts?|revenue|commissions?)\b", re.I
    ),
    "travel_agent": re.compile(r"\b(?:profits?|commissions?|earn(?:ed|ing|ings|s)?|income)\b", re.I),
    "customer": re.compile(r"\b(?:spent|spend|spending|expenses?|costs?|paid|pay|profits?)\b", re.I),
}
_FIRST_PERSON = re.compile(r"\b(?:i|my|me|we|our)\b", re.IGNORECASE)
_ROLE_WORDS: tuple[tuple[re.Pattern[str], Kind], ...] = (
    (re.compile(r"\blo[o]?se affiliates?\b", re.I), "lose_affiliate"),
    (re.compile(r"\btravel agents?\b|\bagents?\b|\bagency\b", re.I), "travel_agent"),
    (re.compile(r"\baffiliates?\b|\boperators?\b", re.I), "affiliate"),
    (re.compile(r"\bdrivers?\b|\bchauffeurs?\b", re.I), "driver"),
    (re.compile(r"\bcustomers?\b|\bclients?\b|\briders?\b|\bpassengers?\b", re.I), "customer"),
)
# Words that say nothing about whose bookings: what is left is the party's name.
_FILLER = frozenset(
    {
        "how", "much", "many", "what", "whats", "is", "was", "are", "were", "be", "been", "being",
        "the", "a", "an", "total", "totals", "overall", "all", "of", "for", "from", "by", "on",
        "in", "with", "did", "does", "do", "has", "have", "had", "we", "our", "ours", "us", "my",
        "me", "i", "you", "your", "platform", "company", "admin", "so", "far", "till", "now",
        "until", "bookings", "booking", "rides", "ride", "trips", "trip", "get", "gets", "got",
        "make", "makes", "made", "making", "earn", "earns", "earned", "earning", "earnings",
        "profit", "profits", "income", "revenue", "margin", "margins", "commission", "commissions",
        "payout", "payouts", "expense", "expenses", "spent", "spend", "spending", "pay", "paid",
        "cost", "costs", "share", "shares", "there", "tell", "show", "give", "know", "want", "need",
        "please", "can", "could", "would", "net", "gross", "money", "amount", "sum", "altogether",
        "it", "this", "that", "as", "at", "to", "s",
    }
)  # fmt: skip
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class Period:
    condition: str  # on r.pickup_date
    label: str  # " this month"


_MONTHS = frozenset(
    {
        "january", "february", "march", "april", "may", "june", "july", "august", "september",
        "october", "november", "december",
    }
)  # fmt: skip
_FIXED_PERIODS: tuple[tuple[re.Pattern[str], Period], ...] = (
    (re.compile(r"\btoday\b", re.I), Period("r.pickup_date = CURDATE()", " today")),
    (re.compile(r"\byesterday\b", re.I), Period("r.pickup_date = CURDATE() - INTERVAL 1 DAY", " yesterday")),
    (
        re.compile(r"\bthis week\b", re.I),
        Period("YEARWEEK(r.pickup_date, 1) = YEARWEEK(CURDATE(), 1)", " this week"),
    ),
    (
        re.compile(r"\blast week\b", re.I),
        Period("YEARWEEK(r.pickup_date, 1) = YEARWEEK(CURDATE() - INTERVAL 1 WEEK, 1)", " last week"),
    ),
    (
        re.compile(r"\bthis month\b", re.I),
        Period(
            "r.pickup_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01') "
            "AND r.pickup_date < DATE_FORMAT(CURDATE(), '%Y-%m-01') + INTERVAL 1 MONTH",
            " this month",
        ),
    ),
    (
        re.compile(r"\blast month\b", re.I),
        Period(
            "r.pickup_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01') - INTERVAL 1 MONTH "
            "AND r.pickup_date < DATE_FORMAT(CURDATE(), '%Y-%m-01')",
            " last month",
        ),
    ),
    (re.compile(r"\bthis year\b", re.I), Period("YEAR(r.pickup_date) = YEAR(CURDATE())", " this year")),
    (re.compile(r"\blast year\b", re.I), Period("YEAR(r.pickup_date) = YEAR(CURDATE()) - 1", " last year")),
)
_MONTH_PERIOD = re.compile(
    rf"\b(?:in\s+(?P<month>{'|'.join(_MONTHS)})|(?P<month_y>{'|'.join(_MONTHS)})\s+(?=20\d\d\b))"
    r"(?:\s+(?P<year>20\d\d))?\b",
    re.IGNORECASE,
)
_YEAR_PERIOD = re.compile(r"\b(?:in\s+)?(?P<year>20\d\d)\b", re.IGNORECASE)
# Time words the periods above don't cover: leave those questions to the model.
_OTHER_TIME = re.compile(
    r"\b(?:since|before|after|between|until|till|ago|days?|weeks?|months?|years?|quarters?|q[1-4]|weekend|"
    r"tonight|tomorrow)\b|\d{1,2}/\d{1,2}|\d{4}-\d{1,2}",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class BookingMoney:
    kind: Literal["breakdown", "taxes", "total"]
    booking: int


@dataclass(frozen=True, slots=True)
class Totals:
    asker: Asker
    party: tuple[str, ...]  # name words of whose bookings (admin only); empty = all in scope
    party_name: str
    kind: Kind | None  # the role named with the party ("affiliate darwin")
    period: Period | None


MoneyQuestion = BookingMoney | Totals


@dataclass(frozen=True, slots=True)
class MoneyAnswer:
    """The whole reply, plus the main query and its rows."""

    message: str
    sql: str | None = None
    rows: QueryRows | None = None


def is_stripe_question(question: str) -> bool:
    return _STRIPE.search(question) is not None


def asker_for(role: str) -> Asker | None:
    return _ASKERS.get(role)


def parse_money_question(question: str, earlier: Sequence[str], asker: Asker | None) -> MoneyQuestion | None:
    """A question these fixed queries answer, or None to leave it to the rest of the agent."""
    return parse_booking_money(question, earlier) or (parse_totals(question, asker) if asker else None)


def parse_booking_money(question: str, earlier: Sequence[str] = ()) -> BookingMoney | None:
    """'tell me the rate for 2109 booking', 'taxes in booking 2109', 'grand total for #2109'.

    A short follow-up without a number ("how much taxes are there?") is about the booking
    the previous question named.
    """
    if _TAXES.search(question):
        kind: Literal["breakdown", "taxes", "total"] = "taxes"
    elif _BREAKDOWN.search(question):
        kind = "breakdown"
    elif _TOTAL.search(question) and not _NOT_THE_TOTAL.search(question):
        kind = "total"
    else:
        return None
    booking = booking_number(question)
    if (
        booking is None
        and earlier
        and len(question.split()) <= _FOLLOW_UP_WORDS
        and not _ELSEWHERE.search(question)
    ):
        booking = booking_number(earlier[-1])
    return BookingMoney(kind, booking) if booking is not None else None


def booking_number(text: str) -> int | None:
    match = _BOOKING_NUMBER.search(text)
    if match is None:
        return None
    return int(match.group("after") or match.group("hash") or match.group("before"))


def parse_totals(question: str, asker: Asker) -> Totals | None:
    """'how much profit for dg limo bookings', "what's my income?", 'how much have I spent?'."""
    if (
        booking_number(question) is not None
        or not _TOTALS_WORDS[asker].search(question)
        or _RANKING.search(question)
    ):
        return None
    if (
        asker == "customer"
        and not _FIRST_PERSON.search(question)
        and not re.search(r"\bexpenses?\b", question, re.I)
    ):
        return None
    text = question.lower()
    period: Period | None = None
    for pattern, fixed in _FIXED_PERIODS:
        if (match := pattern.search(text)) is not None:
            period, text = fixed, text[: match.start()] + " " + text[match.end() :]
            break
    if period is None and (match := _MONTH_PERIOD.search(text)) is not None:
        month = _MONTHS.index((match.group("month") or match.group("month_y")).lower()) + 1
        year = match.group("year")
        when = f"YEAR(r.pickup_date) = {year}" if year else "YEAR(r.pickup_date) = YEAR(CURDATE())"
        label = f" in {_MONTHS[month - 1].capitalize()}" + (f" {year}" if year else "")
        period = Period(f"MONTH(r.pickup_date) = {month} AND {when}", label)
        text = text[: match.start()] + " " + text[match.end() :]
    if period is None and (match := _YEAR_PERIOD.search(text)) is not None:
        year = match.group("year")
        period = Period(f"YEAR(r.pickup_date) = {year}", f" in {year}")
        text = text[: match.start()] + " " + text[match.end() :]
    if _OTHER_TIME.search(text):
        return None
    kind: Kind | None = None
    for pattern, role in _ROLE_WORDS:
        if pattern.search(text):
            kind = kind or role
            text = pattern.sub(" ", text)
    party = tuple(w for w in _WORD.findall(text) if w not in _FILLER)
    if asker != "admin" and (party or kind):
        return None  # scoped users only total their own bookings
    if asker == "admin" and kind is not None and not party:
        return None  # "profit from affiliates" (a breakdown by kind) isn't one of these totals
    return Totals(asker, party, " ".join(party).title(), kind, period)


async def answer_money(
    asked: MoneyQuestion, run: Run, *, choice: str | None = None, account_ids: Sequence[int] = ()
) -> MoneyAnswer:
    """Run the fixed queries. ``choice`` answers a "which one?" menu about the party;
    ``account_ids`` are an affiliate's own accounts."""
    if isinstance(asked, BookingMoney):
        if asked.kind == "total":
            return await _booking_total(asked.booking, run)
        return await _booking_lines(asked.booking, run, taxes_only=asked.kind == "taxes")
    return await _totals(asked, run, choice, account_ids)


async def _booking_total(booking: int, run: Run) -> MoneyAnswer:
    # The named booking whatever its status: only the id (and the requester's scope).
    sql = (
        "SELECT r.id, o.grand_total, UPPER(o.currency) AS currency, r.booking_status "
        f"FROM tbl_reservations r LEFT JOIN tbl_order o ON o.reservation_id = r.id WHERE r.id = {booking}"
    )
    rows = await run(sql)
    if not rows.rows:
        return MoneyAnswer(f"I couldn't find booking #{booking}.", sql, rows)
    _, total, currency, status = rows.rows[0]
    if total is None:
        message = f"No price is recorded for booking #{booking}."
    else:
        message = f"The grand total for booking #{booking} is {currency} {_amount(total)}."
    return MoneyAnswer(message + _status_note(status), sql, rows)


async def _booking_lines(booking: int, run: Run, *, taxes_only: bool) -> MoneyAnswer:
    only_taxes = " AND d.rate_category IN ('taxes', 'direct_taxes')" if taxes_only else ""
    sql = (
        "SELECT d.rate_category, d.rate_label, d.amount, UPPER(o.currency) AS currency "
        "FROM tbl_order o JOIN tbl_order_details d ON d.order_id = o.id "
        f"WHERE o.reservation_id = {booking} AND d.amount <> 0{only_taxes} "
        "ORDER BY FIELD(d.rate_category, 'all_inclusive_rates', 'amenities', 'misc', 'taxes', "
        "'direct_taxes'), d.id"
    )
    rows = await run(sql)
    if not rows.rows:
        found = await run(f"SELECT r.id, r.booking_status FROM tbl_reservations r WHERE r.id = {booking}")
        if not found.rows:
            return MoneyAnswer(f"I couldn't find booking #{booking}.", sql, rows)
        status = _status_note(found.rows[0][1])
        if taxes_only:
            return MoneyAnswer(f"No taxes are recorded for booking {booking}.{status}", sql, rows)
        return MoneyAnswer(f"No price breakdown is recorded for booking #{booking}.{status}", sql, rows)
    lines = [f"- {label}: {currency} {_amount(amount)}" for _, label, amount, currency in rows.rows]
    if taxes_only:
        currency = rows.rows[0][3]
        total = sum((Decimal(str(row[2])) for row in rows.rows), Decimal(0))
        text = [f"Taxes for booking #{booking}:", *lines, f"Total taxes: {currency} {_amount(total)}"]
    else:
        text = [f"Price breakdown for booking #{booking}:", *lines]
    return MoneyAnswer("\n".join(text), sql, rows)


_SHARE_KEYS = {
    "grand_total": "grandTotal",
    "admin_share": "adminShare",
    "affiliate_share": "affiliateShare",
    "travel_agent_share": "travelAgentShare",
    "stripe_fee": "stripeFee",
}
# What each asker may see; admins see the whole split, with their profit first.
_ASKER_SHARES: dict[Asker, tuple[str, ...]] = {
    "admin": ("admin_share", "grand_total", "affiliate_share", "travel_agent_share", "stripe_fee"),
    "affiliate": ("affiliate_share",),
    "travel_agent": ("travel_agent_share",),
}
_SHARE_LABELS = {
    "admin_share": "Admin profit (admin share)",
    "grand_total": "Grand total",
    "affiliate_share": "Affiliate share",
    "travel_agent_share": "Travel agent share",
    "stripe_fee": "Stripe fee",
}
_TITLES: dict[Asker, str] = {
    "admin": "Admin profit",
    "affiliate": "Your income (affiliate share)",
    "travel_agent": "Your commission (travel agent share)",
    "customer": "Your total spend",
}


async def _totals(asked: Totals, run: Run, choice: str | None, account_ids: Sequence[int]) -> MoneyAnswer:
    where = f"r.status = 'enable' AND {_EXCLUDED}"
    who = ""
    if asked.party:
        people = await find_people(asked.party, asked.kind, run)
        if not people:
            return MoneyAnswer(
                f"I couldn't find an affiliate, travel agent or customer named {asked.party_name}."
            )
        picked = (
            people[0] if len(people) == 1 else pick_person(choice, people) if choice is not None else None
        )
        if picked is None:
            return MoneyAnswer(ask_which(asked.party_name, people))
        where += f" AND {booking_filter(picked)}"
        who = f" for {picked.name} ({describe(picked)})"
    if asked.asker == "affiliate" and account_ids:
        where += f" AND r.affiliate_id IN ({', '.join(str(int(i)) for i in account_ids)})"
    if asked.period is not None:
        where += f" AND {asked.period.condition}"
    period = asked.period.label if asked.period else ""
    heading = f"{_TITLES[asked.asker]}{who}{period}, excluding cancelled/refunded bookings:"
    source = "FROM tbl_reservations r JOIN tbl_order o ON o.reservation_id = r.id"
    grouped = "GROUP BY UPPER(o.currency) ORDER BY bookings DESC"

    if asked.asker == "customer":
        sql = (
            "SELECT UPPER(o.currency) AS currency, COUNT(*) AS bookings, SUM(o.grand_total) AS total_spent "
            f"{source} WHERE {where} {grouped}"
        )
        rows = await run(sql)
        if not rows.rows:
            return MoneyAnswer(f"You have no bookings{period} (excluding cancelled/refunded).", sql, rows)
        lines = [f"- {cur}: {_amount(total)} ({_bookings(n)})" for cur, n, total in rows.rows]
        return MoneyAnswer("\n".join([heading, *lines]), sql, rows)

    shares = _ASKER_SHARES[asked.asker]
    sums = ", ".join(
        f"SUM(CAST(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(r.share_array, '$.{_SHARE_KEYS[s]}')), 0) "
        f"AS DECIMAL(12,2))) AS {s}"
        for s in shares
    )
    sql = (
        f"SELECT UPPER(o.currency) AS currency, COUNT(*) AS bookings, {sums} "
        f"{source} WHERE {where} AND {_HAS_SHARES} {grouped}"
    )
    rows = await run(sql)
    skipped_rows = await run(
        f"SELECT COUNT(*) AS skipped FROM tbl_reservations r WHERE {where} AND NOT ({_HAS_SHARES})"
    )
    skipped = int(skipped_rows.rows[0][0] or 0) if skipped_rows.rows else 0
    note = (
        [f"{_bookings(skipped)} had no share data and {'is' if skipped == 1 else 'are'} not included."]
        if skipped
        else []
    )
    if not rows.rows:
        empty = f"No bookings{who}{period} with share data were found (excluding cancelled/refunded)."
        return MoneyAnswer("\n".join([empty, *note]), sql, rows)
    blocks: list[str] = [heading]
    for currency, count, *values in rows.rows:
        if asked.asker == "admin":
            blocks.append(f"{currency} — {_bookings(count)}")
            blocks += [f"- {_SHARE_LABELS[s]}: {_amount(v)}" for s, v in zip(shares, values, strict=True)]
        else:
            blocks.append(f"- {currency}: {_amount(values[0])} ({_bookings(count)})")
    return MoneyAnswer("\n".join([*blocks, *note]), sql, rows)


def _status_note(status: str | None) -> str:
    if status not in _NOT_ACTIVE:
        return ""
    return f" Note: this booking's status is {BOOKING_STATUS_LABELS.get(status, status).lower()}."


def _bookings(count: int) -> str:
    return f"{count} booking{'' if int(count) == 1 else 's'}"


def _amount(value: object) -> str:
    return f"{Decimal(str(value)):,.2f}"
