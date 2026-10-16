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
from app.services.data.policy import ADMIN_POLICY, rider_policy
from app.services.data.sql_guard import SqlRejectedError, TableView, make_safe
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
    assert ADMIN_POLICY.visible_columns("tbl_reservations", cols) == [
        "id", "booking_status", "quote_amount", "affiliate_charged_amount",
    ]  # fmt: skip
    assert ADMIN_POLICY.visible_columns("oauth_access_tokens", ["id"]) == []
    rider = rider_policy([1])
    assert rider.visible_columns("tbl_reservations", cols) == ["id", "booking_status", "quote_amount"]
    assert rider.visible_columns("users", ["id", "name"]) == []


def test_rider_without_account_sees_no_rows() -> None:
    access = rider_policy([]).access("tbl_reservations")
    assert access is not None
    assert access.scope is not None
    assert access.scope.sql() == "FALSE"


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
    assert "ON, OFF or DATA?" in llm.checks[0]["messages"][1]["content"]


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
