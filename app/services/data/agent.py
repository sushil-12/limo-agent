"""Turns a data question into a guarded SQL query and its result.

1. Build the requester's access policy (which tables, columns, rows).
2. Pick the few relevant tables and describe them to the model.
3. The model writes one SELECT (temperature 0).
4. ``sql_guard`` validates and scopes it; the database runs it with a time limit.
5. On a rejection or database error the message goes back to the model, which gets
   ``max_attempts`` tries in total.

A question asked before skips steps 3 and 5: the SQL that answered it is reused from
``sql_cache`` (still guarded and scoped in step 4). So does a question with a fixed shape
(``templates``), such as "how many affiliates are in Chicago?", a money question
(``money``: a booking's rate, taxes or total, profit / income / spend) and a question
naming a person (``people``).

Follow-ups are first made standalone (``followups``): "how many in washington" after a
question about affiliates in Chicago is asked as "how many affiliates are in washington?",
and an ambiguous place ("Washington") is asked about before anything is looked up.
Questions about Stripe get a fixed reply: its status rules aren't defined yet.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

from sqlalchemy.exc import SQLAlchemyError

from app.core.logging import get_logger
from app.llm.base import LLMMessage, LLMProvider
from app.services.data.catalog import NOTES, SchemaCatalog, enum_values, render_tables, select_tables
from app.services.data.examples import Audience, Example, pick_examples, render_examples
from app.services.data.followups import ask_which_place, standalone_questions
from app.services.data.money import (
    STRIPE_REPLY,
    answer_money,
    asker_for,
    is_stripe_question,
    parse_money_question,
    parse_totals,
)
from app.services.data.people import (
    answer_person_question,
    answering_menu,
    is_bare_choice,
    parse_person_question,
)
from app.services.data.policy import AccessPolicy, PolicyResolver
from app.services.data.query_log import JsonlLog
from app.services.data.runner import QueryFailedError, QueryRows, QueryRunner, QueryTimeoutError
from app.services.data.sql_cache import SqlCache, cache_key, normalise
from app.services.data.sql_guard import SqlRejectedError, TableView, UnknownEnumValueError, make_safe
from app.services.data.templates import template_sql
from app.services.prompts import SCOPED_SQL_RULE, SQL_PROMPT
from app.services.requester import Requester

logger = get_logger(__name__)

DataStatus = Literal["ok", "no_access", "unanswerable", "failed", "timeout"]

_SQL_BLOCK = re.compile(r"```(?:sql|mysql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_SQL_START = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE | re.MULTILINE)
_SQL_MAX_TOKENS = 512
_HISTORY_MESSAGES = 6
_MAX_PROMPT_TABLES = 6
# Money totals read the requester's shares out of share_array. The fixed queries only take
# the requester's own key, so the column is visible to them and never to the model.
_MONEY_COLUMNS = {"tbl_reservations": ("share_array",)}


@dataclass(frozen=True, slots=True)
class DataResult:
    status: DataStatus
    sql: str | None = None  # the model's query, before scoping (for display/logs)
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False
    elapsed_ms: float | None = None
    show_sql: bool = False  # admins see the query; other roles only the result
    tables: list[str] = field(default_factory=list)  # tables described to the model
    attempts: list[Attempt] = field(default_factory=list)
    cached: bool = False  # the SQL came from the learned cache, not the model
    message: str | None = None  # a reply written here (person lookups), shown above any rows
    message_only: bool = False  # the message is the whole reply; rows only go in the data event


@dataclass(frozen=True, slots=True)
class Attempt:
    """One SQL write: the model's query, what actually ran, and why it failed (if it did)."""

    sql: str | None
    executed_sql: str | None = None
    error: str | None = None


class DataAgent:
    def __init__(
        self,
        provider: LLMProvider,
        catalog: SchemaCatalog,
        policies: PolicyResolver,
        runner: QueryRunner,
        *,
        max_rows: int,
        max_attempts: int = 3,
        examples: list[Example] | None = None,
        query_log: JsonlLog | None = None,
        sql_cache: SqlCache | None = None,
        log_content: bool = False,
    ) -> None:
        self._provider = provider
        self._catalog = catalog
        self._policies = policies
        self._runner = runner
        self._max_rows = max_rows
        self._max_attempts = max_attempts
        self._examples = examples or []
        self._query_log = query_log
        self._cache = sql_cache
        self._log_content = log_content

    def add_example(self, example: Example) -> None:
        """Offer a newly saved example from now on, in place of one for the same question."""
        same = (example.audience, normalise(example.question))
        self._examples = [e for e in self._examples if (e.audience, normalise(e.question)) != same]
        self._examples.append(example)

    def supports(self, requester: Requester) -> bool:
        """Whether to offer data lookups to this requester at all."""
        return self._policies.supports(requester)

    def knows(self, requester: Requester, history: Sequence[LLMMessage]) -> bool:
        """Whether the last user message was answered before with a query (cache hit)."""
        if self._cache is None:
            return False
        return self._cache.get(requester.role, cache_key(history, _HISTORY_MESSAGES)) is not None

    async def answer(self, requester: Requester, history: Sequence[LLMMessage]) -> DataResult:
        """Look up the answer to the last user message. Raises LLMError if the model fails."""
        started = time.perf_counter()
        try:
            policy = await self._policies.policy_for(requester)
            schema = await self._catalog.schema() if policy is not None else {}
        except SQLAlchemyError as exc:
            logger.warning("data_query_setup_failed", **requester.log_fields(), error=type(exc).__name__)
            return DataResult("failed")
        if policy is None:
            return DataResult("no_access")
        visible = {
            table: cols
            for table, columns in schema.items()
            if (cols := policy.visible_columns(table, [c.name for c in columns]))
        }
        if not visible:
            return DataResult("no_access")

        raw_question = history[-1].content
        asked = standalone_questions(history)
        question = asked[-1] if asked else raw_question
        asked_before = asked[:-1]
        if question != raw_question:
            history = [*history[:-1], LLMMessage("user", question)]
        tables = select_tables(question, visible, limit=_MAX_PROMPT_TABLES)
        shown = {t: [c for c in schema[t] if c.name in set(visible[t])] for t in tables}
        audience: Audience = "scoped" if policy.scoped else "admin"
        system = SQL_PROMPT.format(
            audience=policy.audience,
            scope_rule=SCOPED_SQL_RULE if policy.scoped else "",
            examples=render_examples(pick_examples(self._examples, question, audience, tables=visible)),
            today=dt.datetime.now(dt.UTC).strftime("%A %Y-%m-%d"),
            schema=render_tables(tables, shown),
        )
        messages = [LLMMessage("system", system), *history[-_HISTORY_MESSAGES:]]

        def view_for(table: str) -> TableView | None:
            access = policy.access(table)
            if access is None or table not in visible:
                return None
            shown_columns = set(visible[table])
            enums = {
                c.name: values
                for c in schema[table]
                if c.name in shown_columns and (values := enum_values(c.type)) is not None
            }
            notes = NOTES.get(table)
            list_columns = frozenset(notes.list_columns if notes else ()) & shown_columns
            return TableView(visible[table], access.scope, enums, list_columns)

        key = cache_key(history, _HISTORY_MESSAGES)
        attempts: list[Attempt] = []
        try:
            result: DataResult | None = None
            if is_stripe_question(question):
                result = DataResult("unanswerable", message=STRIPE_REPLY, show_sql=policy.full_access)
            elif (which := ask_which_place(question)) is not None:
                result = DataResult("ok", message=which, message_only=True, show_sql=policy.full_access)
            if result is None:
                result = await self._money(
                    requester, question, history, asked_before, view_for, schema, policy, attempts
                )
            if result is None and policy.full_access:
                result = await self._person_lookup(question, history, view_for, schema, policy)
            if result is None and (templated := template_sql(question, asked_before)) is not None:
                result = await self._run_learned(templated, view_for, frozenset(schema), policy, attempts)
            if (
                result is None
                and self._cache is not None
                and (learned := self._cache.get(requester.role, key))
            ):
                result = await self._run_learned(learned, view_for, frozenset(schema), policy, attempts)
                if result is None:
                    await self._cache.forget(requester.role, key, attempts[-1].error or "failed")
            if result is None:
                result = await self._attempt(messages, view_for, frozenset(schema), policy, attempts)
                # A rewritten follow-up or a bare "3" means something else in another conversation.
                learnable = question == raw_question and not is_bare_choice(raw_question)
                if self._cache is not None and learnable and result.status == "ok" and result.sql is not None:
                    await self._cache.remember(requester.role, key, question, result.sql)
        except SQLAlchemyError as exc:  # pool exhausted, connection lost: not the query's fault
            logger.warning("data_query_db_error", **requester.log_fields(), error=type(exc).__name__)
            result = DataResult("failed", show_sql=policy.full_access)
        result = replace(result, tables=tables, attempts=attempts)
        total_ms = round((time.perf_counter() - started) * 1000, 1)
        if self._query_log is not None:
            await self._query_log.write(
                {
                    **requester.log_fields(),
                    "audience": audience,
                    "question": question,
                    "tables": tables,
                    "status": result.status,
                    "cached": result.cached,
                    "sql": result.sql,
                    "attempts": [asdict(a) for a in attempts],
                    "columns": result.columns,
                    "row_count": len(result.rows),
                    "truncated": result.truncated,
                    "query_ms": result.elapsed_ms,
                    "total_ms": total_ms,
                    "model": self._provider.model,
                }
            )
        logger.info(
            "data_query",
            **requester.log_fields(),
            status=result.status,
            cached=result.cached,
            tables=tables,
            rows=len(result.rows),
            truncated=result.truncated,
            query_ms=result.elapsed_ms,
            total_ms=total_ms,
            **({"sql": result.sql} if self._log_content else {}),
        )
        return result

    async def _attempt(
        self,
        messages: list[LLMMessage],
        view_for: Callable[[str], TableView | None],
        known_tables: frozenset[str],
        policy: AccessPolicy,
        attempts: list[Attempt],
    ) -> DataResult:
        sql: str | None = None
        for attempt in range(1, self._max_attempts + 1):
            reply = await self._provider.chat(messages, max_tokens=_SQL_MAX_TOKENS, temperature=0.0)
            sql = extract_sql(reply.content)
            if sql is None:
                attempts.append(Attempt(None, error="model answered NO_SQL / wrote no query"))
                return DataResult("unanswerable")
            safe_sql: str | None = None
            try:
                safe_sql = self._make_safe(sql, view_for, known_tables)
                rows = await self._runner.run(safe_sql)
            except UnknownEnumValueError as exc:
                # A guessed meaning: a retry only swaps in another value that means something else.
                attempts.append(Attempt(sql, safe_sql, str(exc)))
                return DataResult("unanswerable", sql=sql, show_sql=policy.full_access)
            except (SqlRejectedError, QueryFailedError) as exc:
                attempts.append(Attempt(sql, safe_sql, str(exc)))
                logger.info("data_query_retry", attempt=attempt, error=str(exc))
                messages = [
                    *messages,
                    LLMMessage("assistant", reply.content),
                    LLMMessage("user", f"That query failed: {exc}. Reply with a corrected query only."),
                ]
                continue
            except QueryTimeoutError:
                attempts.append(Attempt(sql, safe_sql, "timeout"))
                return DataResult("timeout", sql=sql, show_sql=policy.full_access)
            attempts.append(Attempt(sql, safe_sql))
            return _ok(sql, rows, policy)
        return DataResult("failed", sql=sql, show_sql=policy.full_access)

    async def _run_learned(
        self,
        sql: str,
        view_for: Callable[[str], TableView | None],
        known_tables: frozenset[str],
        policy: AccessPolicy,
        attempts: list[Attempt],
    ) -> DataResult | None:
        """Run a cached query; None if it no longer works, so the model writes a new one."""
        safe_sql: str | None = None
        try:
            safe_sql = self._make_safe(sql, view_for, known_tables)
            rows = await self._runner.run(safe_sql)
        except (SqlRejectedError, QueryFailedError) as exc:
            attempts.append(Attempt(sql, safe_sql, f"cached query failed: {exc}"))
            return None
        except QueryTimeoutError:
            attempts.append(Attempt(sql, safe_sql, "timeout"))
            return DataResult("timeout", sql=sql, show_sql=policy.full_access, cached=True)
        attempts.append(Attempt(sql, safe_sql))
        return _ok(sql, rows, policy, cached=True)

    async def _person_lookup(
        self,
        question: str,
        history: Sequence[LLMMessage],
        view_for: Callable[[str], TableView | None],
        schema: Mapping[str, Any],
        policy: AccessPolicy,
    ) -> DataResult | None:
        """Answer a question naming a person (see ``people``), or None if it names nobody."""
        choice: str | None = None
        asked = parse_person_question(question)
        if asked is None and (menu := answering_menu(history, parse_person_question)) is not None:
            if menu.clash is not None:
                return DataResult("ok", message=menu.clash, message_only=True, show_sql=policy.full_access)
            asked, choice = menu.question, menu.choice  # the answer to "which one?"
        if asked is None:
            return None
        try:
            found = await answer_person_question(asked, self._fixed_runner(view_for, schema), choice)
        except (SqlRejectedError, QueryFailedError, QueryTimeoutError) as exc:
            logger.warning("person_lookup_failed", error=str(exc)[:200])
            return None  # let the model try
        rows = found.rows
        return DataResult(
            "ok",
            sql=found.sql,
            columns=rows.columns if rows else [],
            rows=rows.rows if rows else [],
            truncated=rows.truncated if rows else False,
            elapsed_ms=rows.elapsed_ms if rows else None,
            show_sql=policy.full_access,
            message=found.message,
        )

    async def _money(
        self,
        requester: Requester,
        question: str,
        history: Sequence[LLMMessage],
        asked_before: Sequence[str],
        view_for: Callable[[str], TableView | None],
        schema: Mapping[str, Any],
        policy: AccessPolicy,
        attempts: list[Attempt],
    ) -> DataResult | None:
        """Answer a money question with fixed SQL (see ``money``), or None if it isn't one."""
        asker = "admin" if policy.full_access else asker_for(requester.role)
        choice: str | None = None
        asked = parse_money_question(question, asked_before, asker)
        if asked is None and asker == "admin":
            menu = answering_menu(
                history, lambda q: t if (t := parse_totals(q, "admin")) and t.party else None
            )
            if menu is not None and menu.clash is None:
                asked, choice = menu.question, menu.choice  # which party the total is for
        if asked is None:
            return None
        run = self._fixed_runner(view_for, schema, extra=_MONEY_COLUMNS)
        try:
            found = await answer_money(asked, run, choice=choice, account_ids=policy.account_ids)
        except (SqlRejectedError, QueryFailedError) as exc:
            # e.g. a rider asking for a price breakdown, which their tables don't hold.
            attempts.append(Attempt(None, error=f"money query failed: {exc}"))
            logger.info("money_query_failed", error=str(exc)[:200])
            return None
        except QueryTimeoutError:
            attempts.append(Attempt(None, error="timeout"))
            return DataResult("timeout", show_sql=policy.full_access)
        rows = found.rows
        return DataResult(
            "ok",
            sql=found.sql,
            columns=rows.columns if rows else [],
            rows=rows.rows if rows else [],
            truncated=rows.truncated if rows else False,
            elapsed_ms=rows.elapsed_ms if rows else None,
            show_sql=policy.full_access,
            message=found.message,
            message_only=True,
        )

    def _fixed_runner(
        self,
        view_for: Callable[[str], TableView | None],
        schema: Mapping[str, Any],
        *,
        extra: Mapping[str, Sequence[str]] | None = None,
    ) -> Callable[[str], Awaitable[QueryRows]]:
        """Runs SQL written here (not by the model) through the guard; ``extra`` columns
        are visible to it besides the requester's own."""

        def view(table: str) -> TableView | None:
            found = view_for(table)
            if found is None or not extra or table not in extra:
                return found
            live = {c.name for c in schema[table]}
            more = [c for c in extra[table] if c in live and c not in found.columns]
            return replace(found, columns=[*found.columns, *more])

        async def run(sql: str) -> QueryRows:
            return await self._runner.run(self._make_safe(sql, view, frozenset(schema)))

        return run

    def _make_safe(
        self, sql: str, view_for: Callable[[str], TableView | None], known_tables: frozenset[str]
    ) -> str:
        safe_sql, _ = make_safe(
            sql,
            view_for,
            known_tables=known_tables,
            max_rows=self._max_rows + 1,  # one extra row tells us the result was cut off
            dialect=self._runner.dialect,
        )
        return safe_sql


def _ok(sql: str, rows: QueryRows, policy: AccessPolicy, *, cached: bool = False) -> DataResult:
    return DataResult(
        "ok",
        sql=sql,
        columns=rows.columns,
        rows=rows.rows,
        truncated=rows.truncated,
        elapsed_ms=rows.elapsed_ms,
        show_sql=policy.full_access,
        cached=cached,
    )


def extract_sql(text: str) -> str | None:
    """The query from a model reply, or None for NO_SQL / no query at all.

    A fenced block is returned as is (the guard rejects anything that isn't a SELECT);
    otherwise the reply must contain a line starting with SELECT or WITH.
    """
    if "NO_SQL" in text.upper():
        return None
    block = _SQL_BLOCK.search(text)
    if block is not None:
        return block.group(1).strip() or None
    start = _SQL_START.search(text)
    return text[start.start() :].strip() if start else None
