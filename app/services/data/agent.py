"""Turns a data question into a guarded SQL query and its result.

1. Build the requester's access policy (which tables, columns, rows).
2. Pick the few relevant tables and describe them to the model.
3. The model writes one SELECT (temperature 0).
4. ``sql_guard`` validates and scopes it; the database runs it with a time limit.
5. On a rejection or database error the message goes back to the model, which gets
   ``max_attempts`` tries in total.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

from sqlalchemy.exc import SQLAlchemyError

from app.core.logging import get_logger
from app.llm.base import LLMMessage, LLMProvider
from app.services.data.catalog import NOTES, SchemaCatalog, enum_values, render_tables, select_tables
from app.services.data.examples import Audience, Example, pick_examples, render_examples
from app.services.data.policy import AccessPolicy, PolicyResolver
from app.services.data.query_log import QueryLog
from app.services.data.runner import QueryFailedError, QueryRunner, QueryTimeoutError
from app.services.data.sql_guard import SqlRejectedError, TableView, make_safe
from app.services.prompts import SCOPED_SQL_RULE, SQL_PROMPT
from app.services.requester import Requester

logger = get_logger(__name__)

DataStatus = Literal["ok", "no_access", "unanswerable", "failed", "timeout"]

_SQL_BLOCK = re.compile(r"```(?:sql|mysql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_SQL_START = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE | re.MULTILINE)
_SQL_MAX_TOKENS = 512
_HISTORY_MESSAGES = 6
_MAX_PROMPT_TABLES = 6


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
        query_log: QueryLog | None = None,
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
        self._log_content = log_content

    def supports(self, requester: Requester) -> bool:
        """Whether to offer data lookups to this requester at all."""
        return self._policies.supports(requester)

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

        question = history[-1].content
        tables = select_tables(question, visible, limit=_MAX_PROMPT_TABLES)
        shown = {t: [c for c in schema[t] if c.name in set(visible[t])] for t in tables}
        audience: Audience = "scoped" if policy.scoped else "admin"
        system = SQL_PROMPT.format(
            audience=policy.audience,
            scope_rule=SCOPED_SQL_RULE if policy.scoped else "",
            examples=render_examples(pick_examples(self._examples, question, audience)),
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

        attempts: list[Attempt] = []
        try:
            result = await self._attempt(messages, view_for, frozenset(schema), policy, attempts)
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
                safe_sql, _ = make_safe(
                    sql,
                    view_for,
                    known_tables=known_tables,
                    max_rows=self._max_rows + 1,  # one extra row tells us the result was cut off
                    dialect=self._runner.dialect,
                )
                rows = await self._runner.run(safe_sql)
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
            return DataResult(
                "ok",
                sql=sql,
                columns=rows.columns,
                rows=rows.rows,
                truncated=rows.truncated,
                elapsed_ms=rows.elapsed_ms,
                show_sql=policy.full_access,
            )
        return DataResult("failed", sql=sql, show_sql=policy.full_access)


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
