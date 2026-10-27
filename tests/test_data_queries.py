"""Data questions: SQL guard rewriting, per-role scoping, self-correction and the API contract."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import orjson
import pytest
import respx
from fastapi.testclient import TestClient
from sqlglot import exp

from app.services.data.agent import extract_sql
from app.services.data.catalog import Column, enum_values, render_tables, select_tables
from app.services.data.policy import ADMIN_POLICY, rider_policy, travel_agent_policy
from app.services.data.sql_guard import SqlRejectedError, TableView, make_safe
from app.services.prompts import build_data_context, build_data_reply
from tests.conftest import DRIVER_ID, RIDER_ID, TokenFactory, auth
from tests.test_chat import FINAL_CHUNK, body, chunk, ndjson, ollama_reply, parse_sse

ADMIN_ID = 12
KNOWN = frozenset({"tbl_reservations", "users", "tbl_account"})
PENDING_SQL = "```sql\nSELECT COUNT(*) AS pending FROM tbl_reservations WHERE booking_status = 'pending'\n```"


# --- SQL guard ----------------------------------------------------------------------


def _views(table: str) -> TableView | None:
    if table == "tbl_reservations":
        return TableView(
            ["id", "booking_status", "status"],
            exp.column("acc_id").isin(exp.Literal.number(7)),
            {
                "booking_status": frozenset({"pending", "cancelled"}),
                "status": frozenset({"enable", "disable"}),
            },
        )
    if table == "tbl_account":
        return TableView(["id", "first_name"], None)
    return None


def _safe(sql: str, max_rows: int = 50) -> str:
    return make_safe(sql, _views, known_tables=KNOWN, max_rows=max_rows)[0]


def test_guard_scopes_every_table_reference_and_caps_rows() -> None:
    out = _safe("SELECT r.booking_status, COUNT(*) FROM tbl_reservations r GROUP BY 1")
    assert (
        "FROM (SELECT `id`, `booking_status`, `status` FROM tbl_reservations WHERE acc_id IN (7)) AS r" in out
    )
    assert out.endswith("LIMIT 50")

    nested = _safe("SELECT * FROM tbl_account WHERE id IN (SELECT id FROM tbl_reservations)")
    assert nested.count("acc_id IN (7)") == 1


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT id FROM tbl_reservations LIMIT 500", "LIMIT 50"),
        ("SELECT id FROM tbl_reservations LIMIT 5", "LIMIT 5"),
        ("SELECT id FROM tbl_reservations UNION SELECT id FROM tbl_account", "LIMIT 50"),
    ],
)
def test_guard_limits(sql: str, expected: str) -> None:
    assert _safe(sql).endswith(expected)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM tbl_reservations",
        "UPDATE tbl_reservations SET booking_status = 'ended'",
        "SELECT 1 FROM tbl_reservations; DROP TABLE users",
        "SELECT SLEEP(10) FROM tbl_reservations",
        "SELECT BENCHMARK(1000000, MD5('x')) FROM tbl_reservations",
        "SELECT LOAD_FILE('/etc/passwd') FROM tbl_reservations",
        "SELECT first_name AS driver_license_number FROM tbl_account",
        "SELECT @@version FROM tbl_reservations",
        "SELECT * FROM tbl_reservations FOR UPDATE",
        "SELECT * FROM users",  # not visible to this requester
        "SELECT * FROM laravel.tbl_reservations",  # schema-qualified
        "SELECT * FROM information_schema.tables",
        "WITH tbl_account AS (SELECT * FROM users) SELECT * FROM tbl_account",  # CTE shadowing
        "SELECT 1",  # no table at all
        "SELECT id FROM tbl_reservations LIMIT x",
        "SELECT * FROM tbl_reservations WHERE id = [your_id]",
        "not sql at all (",
    ],
)
def test_guard_rejects(sql: str) -> None:
    with pytest.raises(SqlRejectedError):
        _safe(sql)


def test_guard_keeps_regexp_as_an_operator() -> None:
    # REGEXP_LIKE() doesn't exist on MySQL 5.7 / MariaDB; the operator works everywhere.
    out = _safe("SELECT id FROM tbl_reservations WHERE booking_status REGEXP 'a' AND id NOT RLIKE '[0-9]'")
    assert "WHERE booking_status REGEXP 'a' AND NOT id REGEXP '[0-9]'" in out
    assert "REGEXP_LIKE" not in out


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM tbl_affiliate_company WHERE step_completed = 2",
        "SELECT id FROM tbl_affiliate_company WHERE step_completed = '2'",
        "SELECT id FROM tbl_affiliate_company WHERE step_completed LIKE '%2%'",
        "SELECT id FROM tbl_affiliate_company WHERE step_completed IN ('2', 2)",
        "SELECT id FROM tbl_affiliate_company WHERE JSON_CONTAINS(step_completed, '2')",
    ],
)
def test_guard_rejects_plain_comparisons_on_number_lists(sql: str) -> None:
    def views(table: str) -> TableView | None:
        return TableView(["id", "step_completed"], None, list_columns=frozenset({"step_completed"}))

    def safe(sql: str) -> str:
        return make_safe(sql, views, known_tables=frozenset({"tbl_affiliate_company"}), max_rows=50)[0]

    with pytest.raises(SqlRejectedError, match="REGEXP"):
        safe(sql)
    assert safe(
        "SELECT COUNT(*) FROM tbl_affiliate_company WHERE step_completed REGEXP '(^|[^0-9])1([^0-9]|$)' "
        "AND step_completed NOT REGEXP '(^|[^0-9])2([^0-9]|$)' OR step_completed IS NULL"
    )


def test_guard_catches_values_on_the_wrong_enum_column() -> None:
    with pytest.raises(SqlRejectedError, match="'cancelled' is a value of booking_status"):
        _safe("SELECT COUNT(*) FROM tbl_reservations WHERE status = 'cancelled' AND status = 'enable'")
    with pytest.raises(SqlRejectedError, match="not a value of booking_status"):
        _safe("SELECT id FROM tbl_reservations r WHERE r.booking_status IN ('pending', 'done')")
    assert _safe("SELECT id FROM tbl_reservations WHERE booking_status = 'Cancelled' AND status = 'enable'")


def test_guard_points_misplaced_columns_to_their_table() -> None:
    sql = (
        "SELECT a.first_name, COUNT(*) n FROM tbl_reservations r JOIN tbl_account a ON a.id = r.id GROUP BY 1"
    )
    assert _safe(sql)  # valid: aliases resolve, n is an output alias
    with pytest.raises(
        SqlRejectedError,
        match="tbl_reservations \\(r\\) has no column first_name; first_name is a column of tbl_account",
    ):
        _safe("SELECT r.first_name FROM tbl_reservations r JOIN tbl_account a ON a.id = r.id")
    with pytest.raises(SqlRejectedError, match="unknown column acc_id"):
        _safe("SELECT id FROM tbl_reservations WHERE acc_id = 3")  # hidden columns don't exist
    with pytest.raises(SqlRejectedError, match="unknown table or alias 'x'"):
        _safe("SELECT x.id FROM tbl_reservations r")


def test_enum_values() -> None:
    assert enum_values("enum('a','it''s')") == frozenset({"a", "it's"})
    assert enum_values("enum('a','b',...)") is None
    assert enum_values("varchar(20)") is None


def test_policy_hides_sensitive_columns_and_other_tables() -> None:
    cols = ["id", "booking_status", "email_token", "ride_otp", "quote_amount", "affiliate_charged_amount"]
    # quote_amount is always empty, so nobody gets it (totals come from tbl_order.grand_total).
    assert ADMIN_POLICY.visible_columns("tbl_reservations", cols) == [
        "id", "booking_status", "affiliate_charged_amount",
    ]  # fmt: skip
    assert ADMIN_POLICY.visible_columns("oauth_access_tokens", ["id"]) == []
    assert ADMIN_POLICY.visible_columns("tbl_affiliate_company", ["id", "stripe_account_type"]) == ["id"]
    rider = rider_policy([1])
    assert rider.visible_columns("tbl_reservations", cols) == ["id", "booking_status"]
    assert rider.visible_columns("users", ["id", "name"]) == []


def test_rider_without_account_sees_no_rows() -> None:
    access = rider_policy([]).access("tbl_reservations")
    assert access is not None
    assert access.scope is not None
    assert access.scope.sql() == "FALSE"


def test_travel_agent_sees_own_bookings_and_only_their_passengers() -> None:
    agent = travel_agent_policy([9])
    bookings = agent.access("tbl_reservations")
    assert bookings is not None
    assert bookings.scope is not None
    assert bookings.scope.sql() == "acc_id IN (9)"
    assert "travel_client_id" in agent.visible_columns(
        "tbl_reservations", ["id", "travel_client_id", "driver_id"]
    )
    passengers = agent.access("tbl_account")
    assert passengers is not None
    assert passengers.scope is not None
    assert (
        passengers.scope.sql() == "id IN (SELECT travel_client_id FROM tbl_reservations WHERE acc_id IN (9))"
    )
    cols = ["id", "first_name", "mobile", "phone_otp", "stripe_customer_id", "latitude"]
    assert agent.visible_columns("tbl_account", cols) == ["id", "first_name", "mobile"]
    assert agent.access("tbl_driver_setting") is None


def test_booking_status_codes_are_shown_in_plain_words() -> None:
    text = build_data_context(
        "ok",
        ["id", "booking_status", "payment_status"],
        [[1, "paid_cash", "paid"], [2, "new_code", None]],
        truncated=False,
    )
    assert "1. Booking number: 1; Booking status: Completed & paid in cash; Payment status: paid" in text
    assert "2. Booking number: 2; Booking status: new_code\n" in text


def test_extract_sql() -> None:
    assert extract_sql(PENDING_SQL).startswith("SELECT COUNT(*)")
    assert extract_sql("Here you go:\nSELECT 1 FROM t") == "SELECT 1 FROM t"
    assert extract_sql("```sql\nDELETE FROM t\n```") == "DELETE FROM t"  # the guard rejects it
    assert extract_sql("NO_SQL") is None
    assert extract_sql("I can't help with that") is None


def test_table_selection_and_rendering() -> None:
    tables = {
        "tbl_reservations": ["id", "booking_status"],
        "tbl_driver_setting": ["id", "first_name"],
        "tbl_airports": ["id", "code"],
        "tbl_vehicle_categories": ["id", "vehicle_cat_name"],
        "users": ["id"],
        "tbl_account": ["id"],
        "tbl_order": ["id"],
    }
    picked = select_tables("how many pending bookings per driver?", tables, limit=3)
    assert picked[:2] == ["tbl_reservations", "tbl_driver_setting"]
    assert "tbl_airports" not in picked
    assert select_tables("hello", tables, limit=3) == ["tbl_reservations"]

    text = render_tables(
        ["tbl_reservations", "tbl_driver_setting"],
        {
            "tbl_reservations": [
                Column("booking_status", "enum('pending','ended')"),
                Column("driver_id", "int"),
            ],
            "tbl_driver_setting": [Column("id", "int")],
        },
    )
    assert "booking_status enum('pending','ended') -- trip progress" in text
    assert "join: tbl_reservations.driver_id = tbl_driver_setting.id" in text


# --- End to end ---------------------------------------------------------------------


class FakeOllama:
    """Routes by num_predict: 3 = classifier, 512 = SQL writer, otherwise the reply."""

    def __init__(
        self, verdict: str = "DATA", sql: list[str] | None = None, reply: str = "You have 2."
    ) -> None:
        self.verdict = verdict
        self.sql = list(sql or [PENDING_SQL])
        self.reply = reply
        self.checks: list[dict[str, Any]] = []
        self.sql_calls: list[dict[str, Any]] = []
        self.turns: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = orjson.loads(request.content)
        predict = payload["options"]["num_predict"]
        if predict == 3:
            self.checks.append(payload)
            return httpx.Response(200, json=ollama_reply(self.verdict))
        if predict == 512:
            self.sql_calls.append(payload)
            sql = self.sql.pop(0) if len(self.sql) > 1 else self.sql[0]
            return httpx.Response(200, json=ollama_reply(sql))
        self.turns.append(payload)
        if payload["stream"]:
            return httpx.Response(200, content=ndjson(chunk(self.reply), FINAL_CHUNK))
        return httpx.Response(200, json=ollama_reply(self.reply))

    @property
    def answer_system_prompt(self) -> str:
        return self.turns[-1]["messages"][0]["content"]

    @property
    def answer_question(self) -> str:
        return self.turns[-1]["messages"][-1]["content"]


ROLES = {1: "admin", 2: "driver", 5: "rider"}


@pytest.fixture
def data_client(client_factory: Callable[..., TestClient]) -> TestClient:
    return client_factory(data_queries_enabled=True, auth_allowed_roles=ROLES)


def fake(ollama: respx.MockRouter, **kwargs: Any) -> FakeOllama:
    handler = FakeOllama(**kwargs)
    ollama.post("/api/chat").mock(side_effect=handler)
    return handler


def ask(client: TestClient, token: str, question: str, *, stream: bool = False) -> httpx.Response:
    return client.post("/v1/chat", json=body(("user", question), stream=stream), headers=auth(token))


@pytest.mark.parametrize(("user_id", "expected"), [(RIDER_ID, 2), (DRIVER_ID, 1), (ADMIN_ID, 4)])
def test_counts_are_scoped_to_the_requester(
    data_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter, user_id: int, expected: int
) -> None:
    llm = fake(ollama)
    response = ask(data_client, make_token(user_id), "How many pending bookings do I have?")

    assert response.status_code == 200
    data = response.json()
    assert data["reply_type"] == "data"
    assert data["data"]["status"] == "ok"
    assert data["data"]["rows"] == [[expected]]
    assert data["data"]["columns"] == ["pending"]
    # Only admins see the SQL; everyone's answer is generated from the result.
    assert (data["data"]["sql"] is not None) == (user_id == ADMIN_ID)
    assert f"The answer is exactly {expected};" in llm.answer_question
    assert not llm.checks  # a clear record question skips the router


def test_sql_prompt_describes_only_visible_columns(
    data_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    llm = fake(ollama)
    ask(data_client, make_token(RIDER_ID), "How many pending bookings do I have?")
    prompt = llm.sql_calls[0]["messages"][0]["content"]
    assert "TABLE tbl_reservations" in prompt
    assert "booking_status" in prompt
    for hidden in ("email_token", "acc_id", "driver_id", "oauth_access_tokens", "users"):
        assert hidden not in prompt
    assert "only their own bookings" in prompt


def test_rider_cannot_reach_hidden_columns_and_model_self_corrects(
    data_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    llm = fake(ollama, sql=["SELECT email_token FROM tbl_reservations", "SELECT FROM", PENDING_SQL])
    data = ask(data_client, make_token(RIDER_ID), "pending bookings?").json()

    assert data["data"]["rows"] == [[2]]
    assert len(llm.sql_calls) == 3
    feedback = llm.sql_calls[1]["messages"][-1]["content"]
    assert feedback.startswith("That query failed:")
    assert "email_token" in feedback
    assert "syntax" in llm.sql_calls[2]["messages"][-1]["content"].lower()


def test_writes_are_never_run(
    data_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter, db_path: Path
) -> None:
    llm = fake(ollama, sql=["```sql\nDELETE FROM tbl_reservations\n```"], reply="Sorry, please rephrase.")
    data = ask(data_client, make_token(ADMIN_ID), "delete all bookings").json()

    assert data["data"]["status"] == "failed"
    assert len(llm.sql_calls) == 3  # data_max_attempts
    assert "Do not guess any numbers" in llm.answer_question
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tbl_reservations").fetchone() == (5,)


def test_unanswerable_question(
    data_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    fake(ollama, sql=["NO_SQL"])
    data = ask(data_client, make_token(RIDER_ID), "What is my driver's favourite colour?").json()
    assert data["data"]["status"] == "unanswerable"
    assert data["data"]["rows"] == []


def test_streaming_sends_data_event_before_text(
    data_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    fake(ollama)
    events = parse_sse(ask(data_client, make_token(RIDER_ID), "pending bookings?", stream=True).text)
    names = [n for n, _ in events if n != "ping"]
    assert names == ["start", "data", "delta", "done"]
    data = dict(events)["data"]
    assert data["rows"] == [[2]]
    assert data["sql"] is None
    assert dict(events)["done"]["reply_type"] == "data"


def test_general_questions_skip_the_lookup(
    data_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    llm = fake(ollama, verdict="ON", reply="Our sedans seat 3.")
    data = ask(data_client, make_token(RIDER_ID), "How many people fit in a sedan?").json()
    assert data["reply_type"] == "answer"
    assert data["data"] is None
    assert not llm.sql_calls
    assert "You can look up the user's 1800Limo data" in llm.answer_system_prompt


def test_follow_up_is_checked_with_the_question_before_it(
    data_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    llm = fake(ollama)
    history = (
        ("user", "How many pending bookings do I have?"),
        ("assistant", "You have 2."),
        ("user", "and cancelled?"),
    )
    data = data_client.post("/v1/chat", json=body(*history), headers=auth(make_token(RIDER_ID))).json()
    assert data["reply_type"] == "data"
    assert not llm.checks  # with the question before it, "and cancelled?" is a clear record question
    assert "and cancelled?" in llm.sql_calls[0]["messages"][-1]["content"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("and cancelled?", True),
        ("What about last month?", True),
        ("only the paid ones", True),
        ("how many cancelled bookings are there?", False),
        ("and could you also tell me everything about every booking we have had in total", False),
    ],
)
def test_is_follow_up(message: str, expected: bool) -> None:
    from app.services.chat_service import is_follow_up

    assert is_follow_up(message) is expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("tell me the recent pickup date", True),
        ("who is the driver of the booking", True),
        ("what is the most recent booking", True),
        ("need to know how much bookings are upcoming ?", True),
        ("status of booking 2096", True),
        ("how do I cancel my booking?", False),
        ("what is your cancellation policy for rides?", False),
        ("how much does a ride to JFK cost?", False),
        ("hi", False),
    ],
)
def test_is_record_question(message: str, expected: bool) -> None:
    from app.services.chat_service import is_record_question

    assert is_record_question(message) is expected


def test_one_record_is_shown_as_labelled_fields_with_dates_written_out() -> None:
    import datetime as dt

    text = build_data_context(
        "ok",
        ["id", "pickup_date", "dropoff", "booking_status", "created_at"],
        [[2105, dt.date(2026, 10, 18), "", "assigned", dt.datetime(2026, 10, 15, 14, 59, 43)]],
        truncated=False,
    )
    assert (
        "Booking number: 2105\nPickup date: 18 Oct 2026\nBooking status: Confirmed, operator assigned" in text
    )
    assert "Created at: 15 Oct 2026, 14:59" in text
    assert "Dropoff" not in text


def test_off_topic_still_answers_normally_when_filter_disabled(
    data_client: TestClient, make_token: TokenFactory, ollama: respx.MockRouter
) -> None:
    llm = fake(ollama, verdict="OFF", reply="Hi")
    data = ask(data_client, make_token(RIDER_ID), "hello").json()
    assert data["reply_type"] == "answer"
    assert not llm.sql_calls


def test_guests_never_query(client_factory: Callable[..., TestClient], ollama: respx.MockRouter) -> None:
    client = client_factory(data_queries_enabled=True)
    llm = fake(ollama, reply="Please sign in.")
    response = client.post(
        "/v1/guest/chat", json=body(("user", "How many bookings do I have?"), guest_name="Rahul")
    )
    assert response.json()["reply_type"] == "answer"
    assert not llm.checks
    assert not llm.sql_calls


# --- Review loop: query log and examples ------------------------------------------------


def test_bundled_examples_load_and_match_by_audience() -> None:
    from app.services.data.examples import load_examples, pick_examples

    examples = load_examples()
    assert len(examples) >= 10
    picked = pick_examples(examples, "show my last 5 bookings", "scoped")
    assert picked[0].question == "show my last 3 bookings"
    assert all(e.audience == "scoped" for e in picked)
    assert pick_examples(examples, "how many?", "admin") == []  # only stop words: no examples


def test_examples_using_tables_the_requester_cannot_query_are_skipped() -> None:
    from app.services.data.examples import load_examples, pick_examples

    examples = load_examples()
    question = "list my passengers with their phone numbers"
    agent_tables = ["tbl_reservations", "tbl_account", "tbl_passengers"]
    assert pick_examples(examples, question, "scoped", tables=agent_tables)[0].question == question
    rider = pick_examples(examples, question, "scoped", tables=["tbl_reservations", "tbl_passengers"])
    assert all("tbl_account" not in e.tables for e in rider)


def test_save_example_appends_then_replaces_the_same_question(tmp_path: Path) -> None:
    from app.services.data.examples import load_examples, save_example

    path = tmp_path / "examples.jsonl"
    path.write_text(
        '// a comment\n{"audience": "admin", "question": "bookings by status", "sql": "SELECT 1"}\n'
    )

    _, replaced = save_example(
        "admin", "How many drivers?", "SELECT  COUNT(*)\n FROM tbl_driver_setting", path
    )
    assert not replaced
    example, replaced = save_example("admin", "bookings by status!", "SELECT 2", path)
    assert replaced
    assert example.sql == "SELECT 2"
    # Same words for another audience is a separate example.
    save_example("scoped", "bookings by status", "SELECT 3", path)

    lines = path.read_text().splitlines()
    assert lines[0] == "// a comment"
    loaded = [(e.audience, e.question, e.sql) for e in load_examples(path)]
    assert loaded == [
        ("admin", "bookings by status!", "SELECT 2"),
        ("admin", "How many drivers?", "SELECT COUNT(*) FROM tbl_driver_setting"),
        ("scoped", "bookings by status", "SELECT 3"),
    ]


def test_query_log_records_every_attempt(
    client_factory: Callable[..., TestClient],
    make_token: TokenFactory,
    ollama: respx.MockRouter,
    tmp_path: Path,
) -> None:
    log = tmp_path / "logs" / "sql.jsonl"
    client = client_factory(data_queries_enabled=True, auth_allowed_roles=ROLES, data_query_log_path=str(log))
    llm = fake(ollama, sql=["SELECT email_token FROM tbl_reservations", PENDING_SQL])
    ask(client, make_token(RIDER_ID), "How many pending bookings do I have?")

    (record,) = [orjson.loads(line) for line in log.read_text().splitlines()]
    assert record["question"] == "How many pending bookings do I have?"
    assert record["role"] == "rider"
    assert record["audience"] == "scoped"
    assert record["status"] == "ok"
    assert record["row_count"] == 1
    assert "rows" not in record  # result values are never logged
    first, second = record["attempts"]
    assert first["sql"] == "SELECT email_token FROM tbl_reservations"
    assert "email_token" in first["error"]
    assert second["error"] is None
    assert "acc_id IN (1)" in second["executed_sql"]
    # The matching worked example was offered to the model.
    assert "Examples of correct queries:" in llm.sql_calls[0]["messages"][0]["content"]


# --- Learned SQL cache and chat log ---------------------------------------------------


def test_repeated_question_reuses_learned_sql(
    client_factory: Callable[..., TestClient],
    make_token: TokenFactory,
    ollama: respx.MockRouter,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "learned.jsonl"
    client = client_factory(
        data_queries_enabled=True, auth_allowed_roles=ROLES, data_sql_cache_path=str(cache)
    )
    llm = fake(ollama)
    first = ask(client, make_token(RIDER_ID), "How many pending bookings do I have?")
    assert first.json()["data"]["status"] == "ok"
    assert (len(llm.checks), len(llm.sql_calls)) == (0, 1)

    # Same words, different case and punctuation: no guard call, no SQL call.
    again = ask(client, make_token(RIDER_ID), "how many pending bookings do i have")
    assert again.json()["data"]["rows"] == first.json()["data"]["rows"]
    assert (len(llm.checks), len(llm.sql_calls)) == (0, 1)

    # Another role never gets the rider's learned query.
    ask(client, make_token(DRIVER_ID), "How many pending bookings do I have?")
    assert len(llm.sql_calls) == 2

    # Survives a restart.
    restarted = client_factory(
        data_queries_enabled=True, auth_allowed_roles=ROLES, data_sql_cache_path=str(cache)
    )
    ask(restarted, make_token(RIDER_ID), "How many pending bookings do I have?")
    assert len(llm.sql_calls) == 2


def test_learned_sql_that_fails_is_forgotten(
    client_factory: Callable[..., TestClient],
    make_token: TokenFactory,
    ollama: respx.MockRouter,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "learned.jsonl"
    stale = "SELECT COUNT(*) FROM tbl_reservations WHERE no_such_column = 1"
    cache.write_text(
        orjson.dumps({"role": "rider", "key": "how many pending bookings", "sql": stale}).decode()
    )
    client = client_factory(
        data_queries_enabled=True, auth_allowed_roles=ROLES, data_sql_cache_path=str(cache)
    )
    llm = fake(ollama)
    response = ask(client, make_token(RIDER_ID), "How many pending bookings?")
    assert response.json()["data"]["status"] == "ok"
    assert len(llm.sql_calls) == 1  # the model wrote a fresh query
    last = orjson.loads(cache.read_text().splitlines()[-1])
    assert last["sql"] == PENDING_SQL.strip("`").removeprefix("sql\n").strip()


def test_sql_with_a_fixed_date_is_not_learned() -> None:
    from app.llm.base import LLMMessage
    from app.services.data.sql_cache import cache_key, is_reusable

    assert not is_reusable("bookings today", "SELECT id FROM t WHERE pickup_date = '2026-10-22'")
    assert not is_reusable("bookings this year", "SELECT id FROM t WHERE YEAR(pickup_date) = 2026")
    assert is_reusable("bookings on 2026-10-22", "SELECT id FROM t WHERE pickup_date = '2026-10-22'")
    assert is_reusable("bookings today", "SELECT id FROM t WHERE pickup_date = CURDATE()")
    follow_up = [
        LLMMessage("user", "Bookings last week?"),
        LLMMessage("assistant", "You had 3."),
        LLMMessage("user", "And cancelled?"),
    ]
    assert cache_key(follow_up, 6) == "bookings last week || and cancelled"


def test_chat_log_records_each_turn(
    client_factory: Callable[..., TestClient],
    make_token: TokenFactory,
    ollama: respx.MockRouter,
    tmp_path: Path,
) -> None:
    log = tmp_path / "chat.jsonl"
    client = client_factory(data_queries_enabled=True, auth_allowed_roles=ROLES, chat_log_path=str(log))
    fake(ollama, reply="You have 2.")
    response = ask(client, make_token(RIDER_ID), "How many pending bookings do I have?")
    parse_sse(ask(client, make_token(RIDER_ID), "How many pending bookings do I have?", stream=True).text)

    first, second = [orjson.loads(line) for line in log.read_text().splitlines()]
    assert first["request_id"] == response.headers["x-request-id"]
    assert first["question"] == "How many pending bookings do I have?"
    assert first["reply"] == "You have 2."
    assert first["reply_type"] == "data"
    assert first["data_status"] == "ok"
    assert first["role"] == "rider"
    assert "rows" not in first
    assert second["stream"] is True
    assert second["reply"] == "You have 2."


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "how many affiliates are in chicago ?",
            ("'%chicago%'", "COUNT(*) AS total", "'Chicago badge, based"),
        ),
        (
            "list approved affiliates in New York",
            ("'%new york%'", "account_approval = 'accepted'", "LIMIT 20"),
        ),
        ("pending affiliates near San Diego area?", ("'%san diego%'", "account_approval = 'in-progress'")),
    ],
)
def test_affiliates_in_a_place_use_the_template(question: str, expected: tuple[str, ...]) -> None:
    from app.services.data.templates import template_sql

    sql = template_sql(question)
    assert sql is not None
    for part in expected:
        assert part in sql


@pytest.mark.parametrize(
    "question", ["how many affiliates do I have?", "affiliates in chicago'; DROP TABLE users; --", "hi"]
)
def test_other_questions_have_no_template(question: str) -> None:
    from app.services.data.templates import template_sql

    assert template_sql(question) is None


def test_a_follow_up_naming_another_place_reuses_the_affiliate_template() -> None:
    from app.services.data.templates import template_sql

    sql = template_sql("and in washington dc", ["how many affiliates are in chicago ?"])
    assert sql is not None
    assert "LIKE '%washington dc%'" in sql
    assert "REPLACE(REPLACE(LOWER(bc.name), '.', ''), ',', '')" in sql  # 'Washington D.C.' matches
    assert "COUNT(*) AS total" in sql
    assert template_sql("and in washington dc", ["how many bookings are upcoming?"]) is None
    assert template_sql("and cancelled?", ["how many affiliates are in chicago?"]) is None


def test_which_ones_follows_the_place_back_through_follow_ups() -> None:
    from app.services.data.templates import template_sql

    earlier = ["how many affiliates are in chicago ?", "and in washington dc"]
    sql = template_sql("which one are onboarding ?", earlier)
    assert sql is not None
    assert "LIKE '%washington dc%'" in sql
    assert "account_approval = 'in-progress'" in sql
    assert "LIMIT 20" in sql  # a list, not counts
    assert template_sql("which one are onboarding ?", [*earlier, "how many bookings today?"]) is None


def test_guard_turns_a_broken_quote_into_a_rejection() -> None:
    with pytest.raises(SqlRejectedError, match="syntax error"):
        _safe(
            "SELECT id FROM tbl_reservations WHERE status REGEXP '(^|[^0-9])N([^0-9]|$)' AND x REGEXP '(^|["
        )


def test_records_and_tables_are_written_without_the_model() -> None:
    columns = ["area", "approved", "onboarding", "total"]
    one = build_data_reply("ok", columns, [["DC badge, based outside DC", 3, 3, 6]], truncated=False)
    expected = "Here's what I found:\n- Area: DC badge, based outside DC\n- Approved: 3\n- Onboarding: 3"
    assert one == expected + "\n- Total: 6"
    many = build_data_reply("ok", ["id", "booking_status"], [[1, "paid"], [2, "cancelled"]], truncated=True)
    assert many is not None
    assert "- Booking #1: Booking status: Completed & paid" in many
    assert "Showing the first 2" in many
    assert build_data_reply("ok", ["total"], [[31]], truncated=False) is None  # the model phrases one number
    assert build_data_reply("ok", columns, [], truncated=False) is None


# --- Person lookups -----------------------------------------------------------------


def _people_db(
    accounts: list[list[Any]], drivers: list[list[Any]], lose: list[list[Any]] | None = None
) -> tuple[Any, list[str]]:
    """A fake runner: candidate queries return these rows; booking queries count 2 and list one.

    Rows: accounts [id, users.role, name, email, company, as_booker, as_passenger, as_affiliate],
    drivers [id, acc_id, name, company, trips], lose affiliates [ids, name, operator_name, bookings].
    """
    from app.services.data.runner import QueryRows

    ran: list[str] = []

    async def run(sql: str) -> QueryRows:
        ran.append(sql)
        if "FROM tbl_account a" in sql:
            rows = accounts
        elif "FROM tbl_driver_setting d" in sql:
            rows = drivers
        elif "FROM tbl_lose_affiliates l" in sql:
            rows = lose or []
        elif "COUNT(*) AS total" in sql:
            rows = [[2]]
        elif "FROM tbl_reservations r" in sql:
            rows = [[2101, "2026-10-25", "23:45:00", "ORD", "Home", "assigned"]]
        else:
            rows = []
        return QueryRows(["c"] * (len(rows[0]) if rows else 0), rows, False, 1.0)

    return run, ran


async def test_one_person_goes_straight_to_their_bookings_by_the_right_column() -> None:
    from app.services.data.people import answer_person_question, parse_person_question

    asked = parse_person_question("is there any upcoming booking for kevin fleming")
    assert asked is not None
    run, ran = _people_db([[27, 5, "Kevin Fleming", "kevin@x.com", None, 10, 204, 0]], [])
    found = await answer_person_question(asked, run)
    assert found.message == "Kevin Fleming (Customer #27, kevin@x.com) has 2 upcoming bookings:"
    assert "(r.acc_id IN (27) OR r.travel_client_id IN (27))" in ran[-1]
    assert "pickup_address LIKE" not in ran[-1]  # never searches booking text for the name


async def test_several_people_are_listed_and_the_reply_picks_one() -> None:
    from app.services.data.people import PICK_PROMPT, answer_person_question, parse_person_question

    asked = parse_person_question("upcoming bookings for joseph anselmo ?")
    assert asked is not None
    accounts = [
        [1, 1, "Joseph Anselmo", "info@x.com", None, 0, 0, 0],  # admin, no bookings: dropped
        [4, 2, "Joseph Anselmo", "j@x.com", "SKYLIMO", 1, 0, 853],
        [70, 5, "Joseph Anselmo", "j1@x.com", None, 99, 0, 0],
    ]
    drivers = [
        [2, 4, "Joseph Anselmo", "SKYLIMO", 847],  # drives for his own company: same person as #4
        [9, 30, "Joseph Anselmo", "Other Limo", 12],
    ]
    run, ran = _people_db(accounts, drivers)
    question = await answer_person_question(asked, run)
    assert question.message.endswith(PICK_PROMPT)
    assert "1. Joseph Anselmo — Affiliate #4 (SKYLIMO) & driver — 853 bookings" in question.message
    assert "Customer #1" not in question.message
    assert "Driver for SKYLIMO" not in question.message

    merged = await answer_person_question(asked, run, choice="1")
    assert "(r.affiliate_id IN (4) OR r.driver_id IN (2))" in ran[-1]
    assert merged.message.startswith("Joseph Anselmo (Affiliate #4 (SKYLIMO) & driver) has 2")
    driver = await answer_person_question(asked, run, choice="the one with other limo")
    assert driver.message.startswith("Joseph Anselmo (Driver for Other Limo) has 2")
    assert "r.driver_id IN (9)" in ran[-1]
    second = await answer_person_question(asked, run, choice="2")
    assert second.message.startswith("Joseph Anselmo (Customer #70")


@pytest.mark.parametrize(
    ("question", "name", "kind", "when"),
    [
        ("Kevin Fleming's bookings", "Kevin Fleming", None, "all"),
        ("how many past rides of driver joseph anselmo?", "Joseph Anselmo", "driver", "past"),
        ("how many bookings for next week", None, None, None),
        ("how many upcoming rides are with lose affiliates?", None, None, None),
        ("bookings with no driver", None, None, None),
    ],
)
def test_parse_person_question(question: str, name: str | None, kind: str | None, when: str | None) -> None:
    from app.services.data.people import parse_person_question

    asked = parse_person_question(question)
    got = (asked.name, asked.kind, asked.when) if asked is not None else (None, None, None)
    assert got == (name, kind, when)
