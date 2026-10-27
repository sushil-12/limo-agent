"""Fixed queries for question shapes the model gets wrong, filled in from the question.

"How many affiliates are in Chicago?" needs a long query (badge city OR address, labelled
by where the affiliate is based) that a small model can't reliably adapt to a new city.
A template answers it for any place, including follow-ups like "and in Washington DC?"
and "tell me the names and numbers of them" (the same affiliates, now as a list).

The place is a proper slot ("in washington give there name and numbers" -> "washington"),
and a question asking for anything the template doesn't return ("affiliates in chicago
with their vehicles") is left to the model. Templated SQL still goes through
``sql_guard``, so it only runs for requesters who can see these tables.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.services.data.followups import is_ambiguous, place_slot, place_terms, rewrite_place_follow_up

_AFFILIATES = re.compile(r"\baffiliates?\b", re.IGNORECASE)
# "which ones are onboarding?", "show the approved ones", "list them"
_WHICH_ONES = re.compile(r"^\W*(which|who|list|show|name|give|tell)\b", re.IGNORECASE)
_CONTACTS = re.compile(r"\b(names?|numbers?|phones?|contacts?|details)\b", re.IGNORECASE)
_PRONOUN = re.compile(r"\b(them|those|these|they|their|there)\b", re.IGNORECASE)  # "there" for "their"
_COUNT = re.compile(r"\b(how many|count|number of|total)\b", re.IGNORECASE)
_APPROVED = re.compile(r"\b(approved|accepted|active)\b", re.IGNORECASE)
_ONBOARDING = re.compile(r"\b(pending|onboarding|in[- ]progress|not approved)\b", re.IGNORECASE)
# Every word these templates can answer. Anything else asks for more than they return.
_ANSWERABLE = frozenset(
    {
        "how", "many", "much", "count", "number", "numbers", "of", "total", "list", "show", "give",
        "tell", "send", "share", "me", "us", "our", "we", "i", "you", "can", "could", "would",
        "please", "get", "have", "do", "does", "the", "a", "an", "all", "also", "and", "with",
        "who", "which", "what", "whats", "s", "are", "is", "there", "their", "them", "they",
        "these", "those", "one", "ones", "affiliate", "affiliates", "name", "names", "phone",
        "phones", "contact", "contacts", "details", "approved", "accepted", "active", "pending",
        "onboarding", "in", "progress", "not", "near", "around", "from", "based", "located", "at",
        "area", "city", "region", "metro",
    }
)  # fmt: skip
_TOKEN = re.compile(r"[a-z]+|[^\sa-z]")
_PUNCTUATION = frozenset("?.!,'")


@dataclass(frozen=True, slots=True)
class _Asked:
    place: str
    question: str


def template_sql(question: str, earlier: Sequence[str] = ()) -> str | None:
    """The query for a known question shape, or None to let the model write one.

    ``earlier`` are the user's previous questions, oldest first, for follow-ups that
    name a new place ("and in Washington DC?") or narrow or list the last answer
    ("which ones are onboarding?", "their names and numbers").
    """
    question = question.strip()
    if (asked := _parse(question)) is not None:
        if is_ambiguous(asked.place):
            return None  # the agent asks which place first
        return _affiliates_in(asked.place, question, listing=_wants_list(question))
    context = _affiliates_context(_standalone(earlier))
    if context is None:
        return None
    if (rewritten := rewrite_place_follow_up(question, context.question)) is not None:
        return template_sql(rewritten)
    if _is_list_follow_up(question) and not is_ambiguous(context.place):
        # The same affiliates as the last answer, with its filters unless this one names others.
        filters = question if _APPROVED.search(question) or _ONBOARDING.search(question) else context.question
        return _affiliates_in(context.place, filters, listing=True)
    return None


def _parse(question: str) -> _Asked | None:
    """An "affiliates in <place>" question asking only for what the templates return."""
    match = _AFFILIATES.search(question)
    if match is None:
        return None
    slot = place_slot(question, match.end())
    if slot is None or not _answerable(question[: slot.start] + " " + question[slot.end :]):
        return None
    return _Asked(slot.place, question)


def _answerable(text: str) -> bool:
    tokens = _TOKEN.findall(text.lower())
    return all(t in _ANSWERABLE or t in _PUNCTUATION for t in tokens)


def _wants_list(question: str) -> bool:
    return bool(_CONTACTS.search(question) or _WHICH_ONES.search(question) or not _COUNT.search(question))


def _is_list_follow_up(question: str) -> bool:
    """'which ones are onboarding?', 'list them', 'tell me the names and number of them'."""
    if not _answerable(question):
        return False
    if _WHICH_ONES.search(question) and (_APPROVED.search(question) or _ONBOARDING.search(question)):
        return True
    return bool(_PRONOUN.search(question) and (_CONTACTS.search(question) or _WHICH_ONES.search(question)))


def _standalone(earlier: Sequence[str]) -> list[str]:
    """Place-only follow-ups ("and in dc?") written out as the question they follow."""
    out: list[str] = []
    for asked in earlier:
        out.append((rewrite_place_follow_up(asked, out[-1]) if out else None) or asked.strip())
    return out


def _affiliates_context(earlier: Sequence[str]) -> _Asked | None:
    """The latest "affiliates in <place>" question, following list follow-ups back to it."""
    for asked in reversed(earlier):
        if (found := _parse(asked)) is not None:
            return found
        if not _is_list_follow_up(asked):
            return None  # the conversation moved on to something else
    return None


def _plain(sql: str) -> str:
    """Lower case without dots and commas, so 'Washington D.C.' matches 'washington dc'."""
    return f"REPLACE(REPLACE(LOWER({sql}), '.', ''), ',', '')"


def _affiliates_in(place: str, question: str, *, listing: bool) -> str:
    """Counts by area, or the list; ``question`` supplies the approval filter."""
    terms = place_terms(place)
    name = terms.label
    badge = f"{_plain('bc.name')} LIKE '{terms.badge}'" if terms.badge else "FALSE"
    full_address = "CONCAT_WS(' ', a.address, a.street, a.city, a.state, a.zip)"
    address = f"{_plain(full_address)} LIKE '{terms.address}'"
    area = (
        f"CASE WHEN {badge} AND {address} THEN '{name} badge, based in {name}' "
        f"WHEN {badge} THEN '{name} badge, based outside {name}' "
        f"ELSE 'Based in {name}, no {name} badge' END"
    )
    source = (
        "FROM tbl_affiliate_company ac JOIN tbl_account a ON a.id = ac.acc_id "
        "LEFT JOIN tbl_badge_cities bc ON bc.id = ac.badge_city "
        f"WHERE a.status = 'enable' AND ({badge} OR {address})"
    )
    if _APPROVED.search(question):
        source += " AND ac.account_approval = 'accepted'"
    elif _ONBOARDING.search(question):
        source += " AND ac.account_approval = 'in-progress'"
    if not listing:
        # One self-contained row per area: easier to report than one row per area and status.
        return (
            f"SELECT {area} AS area, SUM(ac.account_approval = 'accepted') AS approved, "
            "SUM(ac.account_approval = 'in-progress') AS onboarding, "
            "SUM(COALESCE(ac.account_approval, '') NOT IN ('accepted', 'in-progress')) AS other_status, "
            f"COUNT(*) AS total {source} GROUP BY area ORDER BY total DESC"
        )
    # Phone: the company's cell number, else its dispatch number.
    return (
        "SELECT ac.acc_id, ac.company_name, CONCAT_WS(' ', a.first_name, a.last_name) AS contact, "
        "CASE WHEN COALESCE(ac.cell_number, '') <> '' THEN CONCAT_WS(' ', ac.cell_isd, ac.cell_number) "
        "ELSE CONCAT_WS(' ', ac.dispatchisd, ac.dispatch) END AS phone, "
        "TRIM(SUBSTRING_INDEX(SUBSTRING_INDEX(a.address, ',', -3), ',', 1)) AS based_in, "
        f"ac.account_approval, {area} AS area {source} ORDER BY area, ac.company_name LIMIT 20"
    )
