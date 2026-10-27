"""Regressions from chatbot testing round 2: money answers, Stripe, follow-ups, places and menus.

Three levels:
- through the API on the SQLite stand-in, with orders and shares added (``money_db``), so
  role scoping really runs;
- through ``DataAgent`` with a fake MySQL runner (``AgentHarness``), for multi-turn flows
  whose SQL SQLite can't run; every query still goes through ``sql_guard``;
- single functions (guard rules, place slots, reply formatting).
Numbers on the real database are checked in ``test_integration_money.py``.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlglot import exp

from app.llm.base import ChatResult, LLMMessage, Timings, Usage
from app.services.data.agent import DataAgent
from app.services.data.catalog import Column
from app.services.data.followups import (
    ask_which_place,
    place_slot,
    resolve_place_answer,
    standalone_questions,
)
from app.services.data.money import STRIPE_REPLY, parse_booking_money, parse_totals
from app.services.data.people import PICK_PROMPT, answering_menu, answers_menu, is_bare_choice
from app.services.data.policy import ADMIN_POLICY, AccessPolicy
from app.services.data.runner import QueryRows
from app.services.data.sql_guard import SqlRejectedError, TableView, UnknownEnumValueError, make_safe
from app.services.data.templates import template_sql
from app.services.prompts import build_classifier_input, build_data_reply
from app.services.requester import Requester
from tests.conftest import DRIVER_ID, RIDER_ID, TokenFactory, auth
from tests.test_chat import body
from tests.test_data_queries import ADMIN_ID, ROLES, fake

AGENT_ID = 20  # users.id of a travel agent, account 4
DC_OR_STATE = "Do you mean Washington DC or Washington State?"


# --- API level, on SQLite -----------------------------------------------------------------

MONEY_SCHEMA = """
ALTER TABLE tbl_reservations ADD COLUMN share_array TEXT;
CREATE TABLE tbl_order (id INTEGER PRIMARY KEY, reservation_id INTEGER, sub_total REAL, grand_total REAL,
                        currency TEXT);
CREATE TABLE tbl_order_details (id INTEGER PRIMARY KEY, order_id INTEGER, rate_category TEXT,
                                rate_label TEXT, amount REAL);
INSERT INTO users VALUES (20, 'Tia Agent', 'tia@example.com', 3, 0);
INSERT INTO tbl_account (id, user_id, first_name, status) VALUES (4, 20, 'Tia', 'enable');
"""
A = '{"grandTotal": 150, "adminShare": 18, "affiliateShare": 120, "travelAgentShare": 12, "stripeFee": 7.8}'
B = '{"grandTotal": 250, "adminShare": 30, "affiliateShare": 200, "travelAgentShare": 20, "stripeFee": 12.8}'
C = '{"grandTotal": 100, "adminShare": 25, "affiliateShare": 75, "stripeFee": 3}'  # no travelAgentShare
D = '{"grandTotal": 999, "adminShare": 99, "affiliateShare": 900, "stripeFee": 9}'
# id, acc_id (booker), travel_client_id (passenger), affiliate_id, booking_status, share_array
BOOKINGS = [
    (100, 1, None, 2, "paid", A),
    (101, 4, 1, 2, "paid", B),  # the agent booked it for the rider
    (102, 1, None, 2, "paid", C),
    (103, 1, None, 2, "cancelled", D),  # never in totals
    (104, 3, None, 2, "paid", None),  # no share data
    (2109, 1, None, None, "cancelled", None),
    (2112, 3, None, None, "paid", None),
]
# Order ids don't match booking numbers: booking 2109's order is 2106, order 2109 is booking 2112's.
ORDERS = [
    (1, 100, 150, "USD"),
    (2, 101, 250, "usd"),
    (3, 102, 100, "EUR"),
    (4, 103, 999, "USD"),
    (5, 104, 50, "USD"),
    (2106, 2109, 127.79, "USD"),
    (2109, 2112, 5, "USD"),
]
ORDER_DETAILS = [
    (1, 2106, "all_inclusive_rates", "Base Rate", 123.79),
    (2, 2106, "taxes", "Airport Departure Tax", 4),
    (3, 2106, "misc", "Parking", 0),
    (4, 2106, "direct_taxes", "State Tax", 1.5),
    (5, 2109, "all_inclusive_rates", "Base Rate", 4),
]


@pytest.fixture
def money_db(db_path: Path) -> Path:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(MONEY_SCHEMA)
        conn.executemany(
            "INSERT INTO tbl_reservations (id, acc_id, travel_client_id, affiliate_id, booking_status, "
            "payment_status, pickup_date, status, share_array) VALUES (?, ?, ?, ?, ?, 'paid', '2026-09-10', "
            "'enable', ?)",
            BOOKINGS,
        )
        conn.executemany("INSERT INTO tbl_order VALUES (?, ?, NULL, ?, ?)", ORDERS)
        conn.executemany("INSERT INTO tbl_order_details VALUES (?, ?, ?, ?, ?)", ORDER_DETAILS)
    return db_path


@pytest.fixture
def money_client(money_db: Path, client_factory: Callable[..., TestClient]) -> TestClient:
    return client_factory(data_queries_enabled=True, auth_allowed_roles={**ROLES, 3: "travel_agent"})


def chat(client: TestClient, token: str, *turns: str) -> dict[str, Any]:
    """Post a conversation (user, assistant, user, ...) and return the JSON reply."""
    messages = [("user" if i % 2 == 0 else "assistant", text) for i, text in enumerate(turns)]
    response = client.post("/v1/chat", json=body(*messages), headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def test_1_rate_for_a_booking_goes_through_its_order(
    money_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    llm = fake(ollama)
    data = chat(money_client, make_token(ADMIN_ID), "tell me the rate for 2109 booking?")

    assert data["content"] == (
        "Price breakdown for booking #2109:\n"
        "- Base Rate: USD 123.79\n"
        "- Airport Departure Tax: USD 4.00\n"
        "- State Tax: USD 1.50"
    )  # every non-zero line of order 2106; not order 2109's $4 base rate
    sql = data["data"]["sql"]
    assert "o.reservation_id = 2109" in sql
    assert "order_id = 2109" not in sql
    assert "LIMIT" not in sql
    assert not llm.sql_calls


def test_2_taxes_are_the_tax_lines_of_the_booking(
    money_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    llm = fake(ollama)
    data = chat(money_client, make_token(ADMIN_ID), "how much taxes are there in booking 2109?")

    assert data["content"] == (
        "Taxes for booking #2109:\n"
        "- Airport Departure Tax: USD 4.00\n"
        "- State Tax: USD 1.50\n"
        "Total taxes: USD 5.50"
    )
    sql = data["data"]["sql"]
    assert "rate_category IN ('taxes', 'direct_taxes')" in sql
    assert "quote_amount" not in sql
    assert "*" not in sql  # no invented percentage
    assert not llm.sql_calls

    none = chat(money_client, make_token(ADMIN_ID), "taxes for booking 2112")
    assert none["content"] == "No taxes are recorded for booking 2112."


def test_2_taxes_follow_up_keeps_the_booking_and_is_not_refused(
    money_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    llm = fake(ollama, verdict="OFF")
    data = chat(
        money_client,
        make_token(ADMIN_ID),
        "tell me the rate for 2109 booking?",
        "Price breakdown for booking #2109: ...",
        "how much taxes are there",
    )
    assert data["content"].startswith("Taxes for booking #2109:")
    assert not llm.checks


def test_3_grand_total_of_a_named_booking_ignores_its_status(
    money_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    fake(ollama)
    data = chat(money_client, make_token(ADMIN_ID), "what is the grand total for 2109 booking?")

    assert data["content"] == (
        "The grand total for booking #2109 is USD 127.79. Note: this booking's status is cancelled."
    )
    sql = data["data"]["sql"]
    assert "WHERE r.id = 2109" in sql
    for unwanted in ("booking_status NOT IN", "GROUP BY", "COUNT(", "status = 'enable'"):
        assert unwanted not in sql


@pytest.mark.parametrize(
    "question",
    ["how many affiliates are having stripe errors?", "stripe kya hai", "how many stripe errors"],
)
def test_4_stripe_questions_get_the_fixed_reply(
    money_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter, question: str
) -> None:
    llm = fake(ollama, verdict="OFF")
    data = chat(money_client, make_token(ADMIN_ID), question)

    assert data["content"] == STRIPE_REPLY
    assert data["data"]["sql"] is None
    assert not llm.sql_calls
    assert not llm.checks  # never refused as off-topic


def test_4_an_unknown_enum_value_stops_instead_of_guessing_another() -> None:
    def views(table: str) -> TableView | None:
        enums = {"stripe_account_type": frozenset({"custom", "standard", "other"})}
        return TableView(["id", "stripe_account_type"], None, enums)

    with pytest.raises(UnknownEnumValueError):
        make_safe(
            "SELECT COUNT(*) FROM tbl_affiliate_company WHERE stripe_account_type = 'error'",
            views,
            known_tables=frozenset({"tbl_affiliate_company"}),
            max_rows=50,
        )


def test_6_affiliate_income_is_their_affiliate_share_only(
    money_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    fake(ollama)
    data = chat(money_client, make_token(DRIVER_ID), "what's my income?")

    assert data["content"] == (
        "Your income (affiliate share), excluding cancelled/refunded bookings:\n"
        "- USD: 320.00 (2 bookings)\n"
        "- EUR: 75.00 (1 booking)\n"
        "1 booking had no share data and is not included."
    )
    assert data["data"]["sql"] is None  # scoped users never see SQL
    assert data["data"]["columns"] == ["currency", "bookings", "affiliate_share"]


def test_7_travel_agent_commission_is_their_travel_agent_share(
    money_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    fake(ollama)
    data = chat(money_client, make_token(AGENT_ID), "my commission?")

    assert data["content"] == (
        "Your commission (travel agent share), excluding cancelled/refunded bookings:\n"
        "- USD: 20.00 (1 booking)"
    )
    assert data["data"]["columns"] == ["currency", "bookings", "travel_agent_share"]


def test_8_customer_spend_counts_bookings_made_for_them_too(
    money_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    fake(ollama)
    data = chat(money_client, make_token(RIDER_ID), "how much have I spent?")

    # 100 they booked + 101 their travel agent booked for them; 103 and 2109 are cancelled.
    assert data["content"] == (
        "Your total spend, excluding cancelled/refunded bookings:\n"
        "- USD: 400.00 (2 bookings)\n"
        "- EUR: 100.00 (1 booking)"
    )
    assert "share" not in data["content"].lower()


def test_14_admin_profit_is_split_by_currency_with_the_whole_breakdown(
    money_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    llm = fake(ollama)
    data = chat(money_client, make_token(ADMIN_ID), "how much profit did we make?")

    assert data["content"] == (
        "Admin profit, excluding cancelled/refunded bookings:\n"
        "USD — 2 bookings\n"
        "- Admin profit (admin share): 48.00\n"
        "- Grand total: 400.00\n"
        "- Affiliate share: 320.00\n"
        "- Travel agent share: 32.00\n"
        "- Stripe fee: 20.60\n"
        "EUR — 1 booking\n"
        "- Admin profit (admin share): 25.00\n"
        "- Grand total: 100.00\n"
        "- Affiliate share: 75.00\n"
        "- Travel agent share: 0.00\n"
        "- Stripe fee: 3.00\n"
        "6 bookings had no share data and are not included."
    )
    sql = data["data"]["sql"]
    assert "GROUP BY UPPER(o.currency)" in sql
    assert "->" not in sql
    assert not llm.sql_calls


def test_9_a_place_follow_up_is_not_refused_and_asks_which_washington(
    money_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    llm = fake(ollama, verdict="OFF")
    data = chat(
        money_client,
        make_token(ADMIN_ID),
        "how many affiliates are in chicago?",
        "Here's what I found: ...",
        "how many in washington",
    )
    assert data["content"] == DC_OR_STATE
    assert not llm.checks
    assert not llm.sql_calls


def test_11_a_question_with_a_long_tail_asks_which_washington(
    money_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    llm = fake(ollama)
    data = chat(
        money_client, make_token(ADMIN_ID), "how many affiliate in washington give there name and numbers?"
    )
    assert data["content"] == DC_OR_STATE
    assert not llm.sql_calls


# --- Agent level, with a fake MySQL runner ----------------------------------------------------


def _columns(names: str, enums: dict[str, str] | None = None) -> tuple[Column, ...]:
    enums = enums or {}
    return tuple(Column(n, enums.get(n, "varchar(255)")) for n in names.split())


SCHEMA = {
    "tbl_reservations": _columns(
        "id acc_id travel_client_id affiliate_id lose_affiliate driver_id booking_status payment_status "
        "status pickup_date pickup_time pickup pickup_address dropoff dropoff_address share_array "
        "quote_amount created_at",
        {"booking_status": "enum('pending','paid','cancelled','rejected','refund','assigned')"},
    ),
    "tbl_order": _columns("id reservation_id grand_total currency"),
    "tbl_order_details": _columns("id order_id rate_category rate_label amount"),
    "tbl_account": _columns(
        "id user_id first_name middle_name last_name email address street city state zip status soft_delete"
    ),
    "users": _columns("id role"),
    "tbl_affiliate_company": _columns(
        "id acc_id company_name badge_city cell_isd cell_number dispatchisd dispatch account_approval "
        "stripe_account_type",
        {"account_approval": "enum('in-progress','completed','accepted','rejected')"},
    ),
    "tbl_badge_cities": _columns("id name"),
    "tbl_driver_setting": _columns("id acc_id first_name middle_name last_name"),
    "tbl_lose_affiliates": _columns("id reservation_id name operator_name soft_delete"),
    "tbl_travel_planner": _columns("id acc_id agency_name"),
}


@dataclass
class AgentHarness:
    """A DataAgent over fake parts. ``answers`` maps a SQL pattern to the rows it returns."""

    answers: list[tuple[str, list[list[Any]], list[str] | None]] = field(default_factory=list)
    sql_replies: list[str] = field(default_factory=list)
    policy: AccessPolicy = ADMIN_POLICY
    executed: list[str] = field(default_factory=list)
    model_calls: int = 0

    @property
    def model(self) -> str:
        return "fake"

    async def chat(self, messages: Sequence[LLMMessage], **_: Any) -> ChatResult:
        self.model_calls += 1
        content = self.sql_replies.pop(0) if self.sql_replies else "NO_SQL"
        return ChatResult(content, "fake", Usage(0, 0), Timings(0, None, None, None))

    async def schema(self) -> dict[str, tuple[Column, ...]]:
        return SCHEMA

    def supports(self, requester: Requester) -> bool:
        return True

    async def policy_for(self, requester: Requester) -> AccessPolicy:
        return self.policy

    dialect = "mysql"

    async def run(self, sql: str) -> QueryRows:
        self.executed.append(sql)
        rows, columns = next(
            ((r, c) for pattern, r, c in self.answers if re.search(pattern, sql)), ([], None)
        )
        return QueryRows(columns or ["c"] * (len(rows[0]) if rows else 0), rows, False, 1.0)

    async def ask(self, *turns: str, role: str = "admin") -> Any:
        agent = DataAgent(self, self, self, self, max_rows=20)  # type: ignore[arg-type]
        history = [LLMMessage("user" if i % 2 == 0 else "assistant", t) for i, t in enumerate(turns)]
        return await agent.answer(Requester(role, None, user_id=1), history)


async def test_5_profit_for_a_named_affiliate_resolves_them_and_sums_stored_shares() -> None:
    harness = AgentHarness(
        answers=[
            (
                r"AS as_affiliate",
                [[847, 2, "Darwin Guerra", "d@x.com", "Dg Limo Service LLC", 0, 0, 6]],
                None,
            ),
            (r"AS skipped", [[0]], None),
            # admin share, grand total, affiliate share, travel agent share, Stripe fee
            (r"AS admin_share", [["USD", 6, 273, 1825, 1460, 92, 93.05]], None),
        ]
    )
    result = await harness.ask("how much profit for dg limo bookings")

    assert result.message == (
        "Admin profit for Darwin Guerra (Affiliate #847 (Dg Limo Service LLC)), excluding cancelled/refunded "
        "bookings:\n"
        "USD — 6 bookings\n"
        "- Admin profit (admin share): 273.00\n"
        "- Grand total: 1,825.00\n"
        "- Affiliate share: 1,460.00\n"
        "- Travel agent share: 92.00\n"
        "- Stripe fee: 93.05"
    )
    profit = next(sql for sql in harness.executed if "admin_share" in sql)
    assert "r.affiliate_id IN (847)" in profit
    assert "JSON_UNQUOTE(JSON_EXTRACT(r.share_array, '$.adminShare'))" in profit
    assert "NOT r.booking_status IN ('cancelled', 'rejected', 'refund')" in profit  # as the guard writes it
    for unwanted in ("vehicle", "->"):
        assert unwanted not in profit
    assert harness.model_calls == 0


async def test_10_names_and_numbers_of_them_lists_the_same_affiliates() -> None:
    conversation = [
        "how many affiliates are in chicago?",
        "Here's what I found: ...",
        "how many in washington",
        DC_OR_STATE,
        "DC",
    ]
    harness = AgentHarness(
        answers=[(r"AS area", [["Washington DC badge, based outside Washington DC", 3]], None)]
    )
    await harness.ask(*conversation)
    count = harness.executed[-1]
    assert "LIKE '%washington dc%'" in count
    assert "COUNT(*) AS total" in count

    await harness.ask(*conversation, "Here's what I found: ...", "tell me the names and number of them")
    listing = harness.executed[-1]
    assert "FROM (SELECT" in listing  # guarded
    assert "tbl_affiliate_company" in listing
    assert "LIKE '%washington dc%'" in listing
    for column in ("ac.company_name", "AS contact", "AS phone", "ac.dispatch"):
        assert column in listing
    assert "a.city LIKE" not in listing
    assert harness.model_calls == 0


async def test_11_the_answer_to_which_washington_lists_names_and_numbers() -> None:
    question = "how many affiliate in washington give there name and numbers?"
    harness = AgentHarness()
    await harness.ask(question, DC_OR_STATE, "Washington State")
    listing = harness.executed[-1]
    assert "LIKE '% wa %'" in listing  # the state, by its code in addresses
    assert "give there" not in listing
    assert "AS phone" in listing


DARWIN = [
    (r"AS as_affiliate", [
        [847, 2, "Darwin Guerra", "dg@x.com", "Dg Limo Service LLC", 0, 0, 6],
        [846, 5, "Darwin Guerra", "darwin@x.com", None, 2, 0, 0],
    ], None),
    (r"tbl_driver_setting`?\) AS d\b", [[249, 847, "Darwin Guerra", "Dg Limo Service LLC", 6]], None),
    (r"tbl_lose_affiliates`?\) AS l\b",
     [["4453", "Darwin Limo", None, 1], ["4451", "Darwin Limo", "Darwin", 0]], None),
    (r"COUNT\(\*\) AS total", [[6]], None),
    (r"ORDER BY r\.pickup_date DESC", [
        [1965, "2026-09-01", "10:00:00", "Montage Laguna Beach - 30801 Coast Hwy", "LAX", "paid"],
        [1962, "2026-08-30", "09:00:00", "30801 Coast Hwy - 30801 Coast Hwy, Laguna Beach", "LAX", "paid"],
    ], ["id", "pickup_date", "pickup_time", "pickup", "dropoff", "booking_status"]),
]  # fmt: skip


async def test_13_person_menu_shows_names_real_counts_and_merges_affiliate_and_driver() -> None:
    harness = AgentHarness(answers=DARWIN)
    menu = await harness.ask("booking numbers for Darwin?")

    assert menu.message == (
        "I found several people named Darwin:\n"
        "1. Darwin Guerra — Affiliate #847 (Dg Limo Service LLC) & driver — 6 bookings\n"
        "2. Darwin Guerra — Customer #846, darwin@x.com — 2 bookings\n"
        "3. Darwin Limo — Lose affiliate — 1 booking\n"  # 4451 has no bookings: dropped
        f"{PICK_PROMPT}"
    )
    lose = next(sql for sql in harness.executed if "tbl_lose_affiliates" in sql)
    assert "r.lose_affiliate = l.id" in lose


async def test_12_a_bare_number_answers_the_menu_not_the_booking_list() -> None:
    harness = AgentHarness(answers=DARWIN)
    menu = (await harness.ask("booking numbers for Darwin?")).message
    first = await harness.ask("booking numbers for Darwin?", menu, "1")
    listing = build_data_reply("ok", first.columns, first.rows, truncated=False, heading=first.message)
    assert listing is not None
    assert "- Booking #1965: " in listing
    assert not re.search(r"^\d+\. ", listing, re.MULTILINE)  # not a numbered menu

    turns = ["booking numbers for Darwin?", menu, "1", listing, "3"]
    assert answers_menu([LLMMessage("user" if i % 2 == 0 else "assistant", t) for i, t in enumerate(turns)])
    third = await harness.ask(*turns)
    assert third.message.startswith("Darwin Limo (Lose affiliate) has 6 bookings")
    assert "r.lose_affiliate IN (4453)" in harness.executed[-1]
    assert harness.model_calls == 0


def test_12_a_number_that_is_also_a_listed_booking_is_asked_about() -> None:
    menu = "I found several people named Al:\n1. Al — Customer #5 — 2 bookings\n" + PICK_PROMPT
    turns = ["bookings for Al", menu, "1", "Al has 2 bookings:\n- Booking #1: ...\n- Booking #7: ...", "1"]
    found = answering_menu(
        [LLMMessage("user" if i % 2 == 0 else "assistant", t) for i, t in enumerate(turns)], lambda q: q
    )
    assert found is not None
    assert found.clash == "Do you mean option 1 (Al — Customer #5) or booking #1?"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("3", True), ("option 3", True), ("no. 3", True), ("#2", True), ("the third one", True), ("1965", False),
     ("show booking 3", False)],
)  # fmt: skip
def test_bare_choices(text: str, expected: bool) -> None:
    assert is_bare_choice(text) is expected


# --- Single rules -------------------------------------------------------------------------------


def _views(table: str) -> TableView | None:
    columns = {
        "tbl_order": ["id", "reservation_id", "grand_total", "currency"],
        "tbl_order_details": ["id", "order_id", "rate_category", "rate_label", "amount"],
        "tbl_reservations": ["id", "share_array", "status"],
    }
    return TableView(columns[table], None) if table in columns else None


def _safe(sql: str) -> str:
    return make_safe(sql, _views, known_tables=frozenset({"tbl_order", "tbl_order_details"}), max_rows=50)[0]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT rate_label, amount FROM tbl_order_details WHERE order_id = 2109",
        "SELECT d.amount FROM tbl_order_details d WHERE d.order_id IN (2109)",
        "SELECT grand_total FROM tbl_order o WHERE o.id = 2109",
    ],
)
def test_order_ids_are_never_compared_with_a_booking_number(sql: str) -> None:
    with pytest.raises(SqlRejectedError, match="not a booking number"):
        _safe(sql)


def test_booking_prices_through_the_order_pass_the_guard() -> None:
    assert _safe(
        "SELECT d.rate_label, d.amount FROM tbl_order o JOIN tbl_order_details d ON d.order_id = o.id "
        "WHERE o.reservation_id = 2109"
    )


@pytest.mark.parametrize("arrow", ["->>", "->"])
def test_json_arrow_operators_are_rejected_for_mariadb(arrow: str) -> None:
    with pytest.raises(SqlRejectedError, match="JSON_UNQUOTE"):
        _safe(f"SELECT share_array{arrow}'$.adminShare' FROM tbl_reservations")  # noqa: S608
    assert _safe("SELECT 'a->>b' FROM tbl_reservations WHERE status = 'enable'")  # inside a string is fine


def test_wrong_enum_column_still_gets_a_retry_hint() -> None:
    def views(table: str) -> TableView | None:
        enums = {"booking_status": frozenset({"cancelled"}), "status": frozenset({"enable"})}
        return TableView(["id", "booking_status", "status"], exp.true(), enums)

    with pytest.raises(SqlRejectedError, match="is a value of booking_status") as caught:
        make_safe(
            "SELECT id FROM tbl_reservations WHERE status = 'cancelled'",
            views,
            known_tables=frozenset({"tbl_reservations"}),
            max_rows=5,
        )
    assert not isinstance(caught.value, UnknownEnumValueError)


@pytest.mark.parametrize(
    ("question", "place"),
    [
        ("how many affiliate in washington give there name and numbers?", "washington"),
        ("how many affiliates are in chicago ?", "chicago"),
        ("pending affiliates near San Diego area?", "San Diego"),
        ("affiliates in Portland, OR", "Portland, OR"),
        ("affiliates in chicago'; DROP TABLE users; --", "chicago"),
    ],
)
def test_place_slot_stops_at_words_that_are_not_a_place(question: str, place: str) -> None:
    slot = place_slot(question)
    assert slot is not None
    assert slot.place == place


def test_templates_skip_questions_asking_for_more_than_they_return() -> None:
    assert template_sql("how many affiliates in chicago with their vehicles?") is None
    assert template_sql("affiliates in chicago'; DROP TABLE users; --") is None
    assert template_sql("how many affiliate in washington give there name and numbers?") is None  # ask first
    listing = template_sql("how many affiliate in Washington DC give there name and numbers?")
    assert listing is not None
    assert "LIMIT 20" in listing
    assert "'%washington dc give" not in listing


@pytest.mark.parametrize(
    ("question", "asks"),
    [
        ("how many affiliates in washington", True),
        ("how many affiliates in washington dc", False),
        ("how many affiliates in Washington State?", False),
        ("bookings in portland", True),
        ("upcoming bookings for denzel washington", False),
    ],
)
def test_ambiguous_places_are_asked_about(question: str, asks: bool) -> None:
    assert (ask_which_place(question) is not None) is asks


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("DC", "how many in Washington DC"),
        ("washington d.c.", "how many in Washington DC"),
        ("the state", "how many in Washington State"),
        ("no idea", None),
    ],
)
def test_the_answer_replaces_the_ambiguous_place(answer: str, expected: str | None) -> None:
    assert resolve_place_answer("how many in washington", DC_OR_STATE, answer) == expected


def test_follow_ups_become_standalone_questions() -> None:
    turns = ["how many affiliates are in chicago?", "...", "how many in washington", DC_OR_STATE, "DC"]
    history = [LLMMessage("user" if i % 2 == 0 else "assistant", t) for i, t in enumerate(turns)]
    assert standalone_questions(history) == [
        "how many affiliates are in chicago?",
        "how many affiliates are in washington?",
        "how many affiliates are in Washington DC?",
    ]


@pytest.mark.parametrize(
    ("question", "kind", "booking"),
    [
        ("tell me the rate for 2109 booking?", "breakdown", 2109),
        ("how much taxes are there in booking 2109?", "taxes", 2109),
        ("what is the grand total for 2109 booking?", "total", 2109),
        ("price breakdown of booking #2096", "breakdown", 2096),
        ("how much does the affiliate get for booking 2096?", None, None),  # a share: the model's
    ],
)
def test_booking_money_questions(question: str, kind: str | None, booking: int | None) -> None:
    asked = parse_booking_money(question)
    assert ((asked.kind, asked.booking) if asked else (None, None)) == (kind, booking)


def test_profit_words_depend_on_who_asks() -> None:
    admin = parse_totals("how much profit for dg limo bookings", "admin")
    assert admin is not None
    assert admin.party == ("dg", "limo")
    assert parse_totals("what's my income?", "affiliate") is not None
    assert parse_totals("my commission?", "travel_agent") is not None
    assert parse_totals("how much have I spent?", "customer") is not None
    assert parse_totals("how much income did john bring me?", "affiliate") is None  # only their own total
    assert parse_totals("top drivers by earnings", "admin") is None
    this_month = parse_totals("admin profit this month", "admin")
    assert this_month is not None
    assert this_month.period is not None
    assert this_month.party == ()


def test_classifier_sees_the_earlier_questions() -> None:
    text = build_classifier_input(
        "how many in washington", "Here's what I found", earlier=["how many affiliates are in chicago?"]
    )
    assert "Earlier questions from the user:\n- how many affiliates are in chicago?" in text


def test_duplicated_place_names_are_shown_once() -> None:
    reply = build_data_reply(
        "ok",
        ["id", "pickup", "dropoff"],
        [
            [1962, "30801 Coast Hwy - 30801 Coast Hwy, Laguna Beach, CA 92651, USA", "LAX"],
            [1965, "Montage Laguna Beach - 30801 Coast Hwy, Laguna Beach, CA 92651, USA", "LAX"],
        ],
        truncated=False,
    )
    assert reply is not None
    assert "- Booking #1962: Pickup: 30801 Coast Hwy, Laguna Beach, CA 92651, USA; Dropoff: LAX" in reply
    assert "Pickup: Montage Laguna Beach - 30801 Coast Hwy, Laguna Beach" in reply


@pytest.mark.parametrize("follow_up", ["can you name those?", "their phone numbers", "list them"])
def test_10_naming_the_affiliates_just_counted_is_a_lookup(
    money_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter, follow_up: str
) -> None:
    # The router model answered ON for "can you name those?", so the reply model said it had no names.
    llm = fake(ollama, verdict="ON")
    data = chat(
        money_client,
        make_token(ADMIN_ID),
        "how many affiliates are in chicago?",
        "Here's what I found: ...",
        "how many in washington",
        DC_OR_STATE,
        "Washington DC",
        "Here's what I found: ...",
        follow_up,
    )
    assert data["reply_type"] == "data"  # looked up, not answered from the conversation
    assert not llm.checks


@pytest.mark.parametrize("follow_up", ["can you name those?", "their phone numbers", "list them"])
async def test_10_naming_those_lists_the_affiliates_just_counted(follow_up: str) -> None:
    harness = AgentHarness()
    await harness.ask(
        "how many affiliates are in chicago?",
        "Here's what I found: ...",
        "how many in washington",
        DC_OR_STATE,
        "Washington DC",
        "Here's what I found: ...",
        follow_up,
    )
    listing = harness.executed[-1]
    assert "tbl_affiliate_company" in listing
    assert "LIKE '%washington dc%'" in listing
    assert "AS phone" in listing
    assert harness.model_calls == 0


def test_a_short_unrelated_follow_up_is_still_classified() -> None:
    from app.services.chat_service import is_record_follow_up

    assert is_record_follow_up("can you name those?", ["how many affiliates are in Washington DC?"])
    assert not is_record_follow_up("can you name those?", ["what is your cancellation policy?"])
    assert not is_record_follow_up("what's the weather today", ["how many affiliates are in chicago?"])
