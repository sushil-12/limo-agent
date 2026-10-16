"""Print the SQL the agent writes for a list of questions, to review and correct it.

Runs the real pipeline (table selection, examples, model, guard, database) without the
chat layer, as a given role. Each run is also appended to DATA_QUERY_LOG_PATH if set.

    # one question
    docker compose exec api python -m app.cli.sql_eval --role admin "how many pending bookings"

    # a file of questions (one per line, # comments allowed), as a rider
    docker compose exec -T api python -m app.cli.sql_eval --role rider --user-id 1771 - \
        < docs/sql_eval_questions.txt

    # check that every example in sql_examples.jsonl still runs against the database
    docker compose exec api python -m app.cli.sql_eval --check-examples
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine
from app.llm.base import LLMMessage
from app.llm.factory import create_llm_provider, create_ollama_client
from app.main import _build_data_agent
from app.services.data.catalog import SchemaCatalog
from app.services.data.examples import load_examples
from app.services.data.policy import ADMIN_POLICY
from app.services.data.runner import QueryFailedError, QueryRunner, QueryTimeoutError
from app.services.data.sql_guard import SqlRejectedError, TableView, make_safe
from app.services.requester import Requester

_ROLE_USER = {"admin": 1}  # any id works for admins; scoped roles need a real users.id


def _questions(args: argparse.Namespace) -> list[str]:
    lines = sys.stdin.read().splitlines() if args.questions == ["-"] else args.questions
    return [q.strip() for q in lines if q.strip() and not q.lstrip().startswith("#")]


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging("WARNING", json_logs=False)
    engine = create_engine(settings)
    client = create_ollama_client(settings)
    try:
        if args.check_examples:
            return await _check_examples(engine)
        provider = create_llm_provider(settings.llm_provider, settings, client)
        agent = _build_data_agent(settings, engine, client, provider, log_content=False)
        if agent is None:
            print("DATA_QUERIES_ENABLED is false", file=sys.stderr)
            return 2
        user_id = args.user_id or _ROLE_USER.get(args.role)
        if user_id is None:
            print(f"--user-id is required for role {args.role!r}", file=sys.stderr)
            return 2
        requester = Requester(role=args.role, display_name="eval", user_id=user_id)
        failures = 0
        for number, question in enumerate(_questions(args), start=1):
            result = await agent.answer(requester, [LLMMessage("user", question)])
            failures += result.status != "ok"
            print(f"\n=== {number}. {question}")
            print(f"tables: {', '.join(result.tables) or '-'}")
            for index, attempt in enumerate(result.attempts, start=1):
                mark = "OK " if attempt.error is None else "ERR"
                print(f"[{mark} attempt {index}] {_one_line(attempt.sql or '(no SQL)')}")
                if attempt.error:
                    print(f"          -> {attempt.error}")
                if args.executed and attempt.executed_sql:
                    print(f"          ran: {attempt.executed_sql}")
            preview = result.rows[: args.rows]
            print(f"status: {result.status}  rows: {len(result.rows)}  columns: {result.columns}  {preview}")
        return 1 if failures else 0
    finally:
        await client.aclose()
        await engine.dispose()


async def _check_examples(engine: AsyncEngine) -> int:
    """Run every example unscoped (as admin would) to catch typos and schema drift."""
    runner = QueryRunner(engine, timeout_seconds=10, max_rows=5)
    schema = await SchemaCatalog(engine, ttl_seconds=0).schema()

    def view_for(table: str) -> TableView | None:
        columns = ADMIN_POLICY.visible_columns(table, [c.name for c in schema.get(table, ())])
        return TableView(columns, None) if columns else None

    bad = 0
    for example in load_examples():
        try:
            sql, _ = make_safe(
                example.sql, view_for, known_tables=frozenset(schema), max_rows=5, dialect="mysql"
            )
            rows = await runner.run(sql)
            print(f"OK   [{example.audience}] {example.question}  ->  {rows.rows[:2]}")
        except (SqlRejectedError, QueryFailedError, QueryTimeoutError) as exc:
            bad += 1
            print(f"FAIL [{example.audience}] {example.question}  ->  {type(exc).__name__}: {exc}")
    return 1 if bad else 0


def _one_line(sql: str) -> str:
    return " ".join(sql.split())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("questions", nargs="*", help="questions, or - to read them from stdin")
    parser.add_argument("--role", default="admin", help="admin, sub_admin, rider, driver, affiliate_driver")
    parser.add_argument("--user-id", type=int, help="users.id to scope rider/driver data to")
    parser.add_argument("--rows", type=int, default=3, help="result rows to preview")
    parser.add_argument("--executed", action="store_true", help="also print the scoped SQL that ran")
    parser.add_argument("--check-examples", action="store_true", help="run sql_examples.jsonl and report")
    args = parser.parse_args(argv)
    if not args.questions and not args.check_examples:
        parser.error("give questions, - for stdin, or --check-examples")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
