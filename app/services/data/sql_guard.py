"""Validates model-written SQL and rewrites it so it can only read what policy allows.

Each table reference ``FROM t AS a`` becomes ``FROM (SELECT <visible cols> FROM t
WHERE <scope>) AS a``. Hidden columns then don't exist for the query, and rows outside
the requester's scope can't be reached however the query is written. MySQL merges
these derived tables into the outer query, so indexes on the base tables still apply.

Rejection messages are fed back to the model so it can correct itself; they never
contain data.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

import sqlglot
from sqlglot import exp
from sqlglot.dialects.mysql import MySQL
from sqlglot.errors import ParseError

MAX_SQL_CHARS = 5000


class _MySQLOut(MySQL):
    """MySQL output, but ``a REGEXP b`` stays an operator: sqlglot writes REGEXP_LIKE(a, b),
    which MySQL 5.7 and MariaDB don't have."""

    class Generator(MySQL.Generator):
        TRANSFORMS: ClassVar[dict[type[exp.Expression], Any]] = {
            **MySQL.Generator.TRANSFORMS,
            exp.RegexpLike: lambda self, e: (
                self.function_fallback_sql(e) if e.args.get("flag") else self.binary(e, "REGEXP")
            ),
        }


_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name)
    for name in (
        "Insert", "Update", "Delete", "Merge", "Drop", "Create", "Alter", "TruncateTable", "Command",
        "Into", "Lock", "Set", "SessionParameter", "Parameter", "Placeholder", "JSONTable", "Use",
    )
    if hasattr(exp, name)
)  # fmt: skip
# sqlglot parses functions it doesn't model as Anonymous. Only these unmodelled ones
# are allowed; SLEEP, BENCHMARK, LOAD_FILE, GET_LOCK and the like are not.
_SAFE_ANONYMOUS = frozenset(
    {
        "DAYNAME", "MONTHNAME", "WEEK", "WEEKDAY", "YEARWEEK", "DAYOFMONTH", "DAYOFYEAR", "LAST_DAY",
        "MAKEDATE", "PERIOD_DIFF", "SEC_TO_TIME", "TIME_TO_SEC", "TIMESTAMPADD", "CONVERT_TZ", "FIELD",
        "FORMAT", "LPAD", "RPAD", "INSTR", "LOCATE", "ELT", "JSON_LENGTH", "JSON_CONTAINS", "JSON_UNQUOTE",
        "JSON_EXTRACT", "ANY_VALUE", "TRUNCATE", "SIGN", "MID", "SUBSTRING_INDEX", "CHAR_LENGTH",
        "QUARTER", "UTC_DATE", "UTC_TIMESTAMP", "CURTIME", "ISNULL", "NULLIF", "GREATEST", "LEAST",
    }
)  # fmt: skip


class SqlRejectedError(Exception):
    """The SQL is unsafe or unusable. The message is written for the model."""


@dataclass(frozen=True, slots=True)
class TableView:
    """What a table looks like to this requester."""

    columns: Sequence[str]
    scope: exp.Condition | None
    enums: Mapping[str, frozenset[str]] = field(default_factory=dict)  # column -> allowed values
    list_columns: frozenset[str] = frozenset()  # text lists of numbers, e.g. '["0","1"]' or '[0,1]'


def make_safe(
    sql: str,
    view_for: Callable[[str], TableView | None],
    *,
    known_tables: frozenset[str],
    max_rows: int,
    dialect: str = "mysql",
) -> tuple[str, list[str]]:
    """Return (executable SQL, tables used) or raise SqlRejectedError.

    ``view_for`` returns None for tables the requester may not query; ``known_tables``
    is every real table name, used to stop CTEs from shadowing one.
    """
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise SqlRejectedError("the query is empty")
    if len(sql) > MAX_SQL_CHARS:
        raise SqlRejectedError("the query is too long; write a simpler one")
    try:
        statements = [s for s in sqlglot.parse(sql, read="mysql") if s is not None]
    except ParseError as exc:
        raise SqlRejectedError(f"MySQL syntax error: {str(exc).splitlines()[0][:200]}") from exc
    if len(statements) != 1:
        raise SqlRejectedError("write exactly one SELECT statement")
    root = statements[0]
    if not isinstance(root, exp.Query):
        raise SqlRejectedError("only SELECT queries are allowed")

    for node in root.walk():
        if isinstance(node, exp.Array | exp.Bracket):  # e.g. "WHERE id = [your_id]"
            raise SqlRejectedError(
                "placeholders like [your_id] are not allowed; do not filter by the user's id at all, "
                "the tables already contain only this user's rows"
            )
        if isinstance(node, _FORBIDDEN_NODES):
            raise SqlRejectedError(f"{node.key.upper()} is not allowed; write a plain SELECT")
        if isinstance(node, exp.Anonymous) and node.name.upper() not in _SAFE_ANONYMOUS:
            raise SqlRejectedError(f"function {node.name.upper()} is not allowed")

    known_lower = {t.lower() for t in known_tables}
    ctes = {cte.alias_or_name for cte in root.find_all(exp.CTE)}
    if any(name.lower() in known_lower for name in ctes):
        raise SqlRejectedError("a WITH (CTE) name must not reuse a table name")

    refs: list[tuple[exp.Table, str, TableView]] = []
    for table in list(root.find_all(exp.Table)):
        if not isinstance(table.this, exp.Identifier):
            raise SqlRejectedError("table functions are not allowed")
        if table.args.get("db") or table.args.get("catalog"):
            raise SqlRejectedError("do not prefix table names with a database name")
        name = table.name
        if name in ctes:
            continue
        view = view_for(name)
        if view is None or not view.columns:
            raise SqlRejectedError(f"table {name} is not available; use only the tables listed")
        refs.append((table, name, view))
    if not refs:
        raise SqlRejectedError("the query must read from at least one of the listed tables")

    # Checked before the rewrite, whose scope conditions use columns the model can't see.
    _check_columns(root, refs, has_ctes=bool(ctes))
    enums: dict[str, frozenset[str]] = {}
    for _, _, view in refs:
        for column, values in view.enums.items():
            enums[column] = enums.get(column, frozenset()) | values
    _check_enum_values(root, enums)
    _check_list_columns(root, frozenset().union(*(view.list_columns for _, _, view in refs)))

    used: list[str] = []
    for table, name, view in refs:
        derived = exp.select(*(exp.column(c, quoted=True) for c in view.columns)).from_(
            exp.to_table(name, quoted=True)
        )
        if view.scope is not None:
            derived = derived.where(view.scope.copy())
        table.replace(derived.subquery(table.alias_or_name))
        if name not in used:
            used.append(name)

    out = _MySQLOut if dialect == "mysql" else dialect
    return _cap_rows(root, max_rows).sql(dialect=out), used


def _check_columns(
    root: exp.Query, refs: Sequence[tuple[exp.Table, str, TableView]], *, has_ctes: bool
) -> None:
    """Reject unknown columns with a message naming the table that does have them.

    The database only says "Unknown column 'd.driver_id'"; telling the model where the
    column actually lives lets it fix a join on the next try.
    """
    by_alias = {table.alias_or_name: (name, view) for table, name, view in refs}
    owners: dict[str, list[str]] = {}
    for _, name, view in refs:
        for column in view.columns:
            if name not in owners.setdefault(column.lower(), []):
                owners[column.lower()].append(name)
    output_aliases = {a.alias.lower() for a in root.find_all(exp.Alias) if a.alias}
    subquery_aliases = {s.alias_or_name for s in root.find_all(exp.Subquery) if s.alias_or_name}

    def where_is(column: str) -> str:
        found = owners.get(column.lower())
        return f"; {column} is a column of {', '.join(found)}" if found else ""

    for col in root.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            continue
        name = col.name
        if col.table:
            target = by_alias.get(col.table)
            if target is None:
                if col.table in subquery_aliases or has_ctes:
                    continue
                raise SqlRejectedError(f"unknown table or alias {col.table!r} in {col.sql()}")
            table_name, view = target
            if name.lower() not in {c.lower() for c in view.columns}:
                raise SqlRejectedError(
                    f"{table_name} ({col.table}) has no column {name}{where_is(name)}. "
                    f"{table_name} columns: {', '.join(list(view.columns)[:40])}"
                )
        elif (
            not has_ctes
            and not subquery_aliases
            and name.lower() not in owners
            and name.lower() not in output_aliases
        ):
            raise SqlRejectedError(f"unknown column {name}; use only the listed columns")


def _check_list_columns(root: exp.Query, list_columns: frozenset[str]) -> None:
    """Reject ``col = 2`` / ``col LIKE '%2%'`` / ``JSON_CONTAINS(col, ...)`` on number-list columns.

    They hold '["0","1"]' (strings) or '[0,1]' (ints) with varying spaces, so equality is
    always false, LIKE '%1%' also matches 10, and JSON_CONTAINS misses one of the forms.
    """
    if not list_columns:
        return
    comparisons = (exp.EQ, exp.NEQ, exp.In, exp.Like, exp.ILike, exp.GT, exp.GTE, exp.LT, exp.LTE)
    for node in root.find_all(*comparisons, exp.Anonymous):
        if isinstance(node, exp.Anonymous):
            if node.name.upper() != "JSON_CONTAINS":
                continue
            sides = node.expressions[:1]
        else:
            sides = [node.this, *([] if isinstance(node, exp.In) else [node.expression])]
        column = next((s for s in sides if isinstance(s, exp.Column) and s.name in list_columns), None)
        if column is not None:
            raise SqlRejectedError(
                f"{column.name} is a text list of numbers like '[\"0\",\"1\"]' or '[0,1]'; do not use "
                f"=, LIKE, IN or JSON_CONTAINS on it. Number N is in the list: "
                f"{column.name} REGEXP '(^|[^0-9])N([^0-9]|$)'; not in it: "
                f"{column.name} NOT REGEXP '(^|[^0-9])N([^0-9]|$)'"
            )


def _check_enum_values(root: exp.Query, enums: Mapping[str, frozenset[str]]) -> None:
    """Reject ``col = 'x'`` / ``col IN (...)`` when 'x' isn't one of the enum column's values.

    Models often put a value on the wrong column (status = 'cancelled' instead of
    booking_status = 'cancelled'); such a query runs fine and silently returns 0.
    """
    owners: dict[str, list[str]] = {}
    for column, values in enums.items():
        for value in values:
            owners.setdefault(value.lower(), []).append(column)
    for node in root.find_all(exp.EQ, exp.NEQ, exp.In):
        sides = [node.this, *([] if isinstance(node, exp.In) else [node.expression])]
        column = next((s for s in sides if isinstance(s, exp.Column) and s.name in enums), None)
        if column is None:
            continue
        literals = node.expressions if isinstance(node, exp.In) else sides
        allowed = enums[column.name]
        for literal in literals:
            if not (isinstance(literal, exp.Literal) and literal.is_string):
                continue
            if literal.this.lower() in {v.lower() for v in allowed}:
                continue
            hint = [c for c in owners.get(literal.this.lower(), []) if c != column.name]
            raise SqlRejectedError(
                f"'{literal.this}' is not a value of {column.name} (its values: "
                + ", ".join(f"'{v}'" for v in sorted(allowed))
                + ")"
                + (f"; '{literal.this}' is a value of {', '.join(hint)}" if hint else "")
            )


def _cap_rows(root: exp.Query, max_rows: int) -> exp.Query:
    limit = root.args.get("limit")
    if limit is None:
        return root.limit(max_rows, copy=False)
    value = limit.expression
    if not (isinstance(value, exp.Literal) and value.is_int):
        raise SqlRejectedError("LIMIT must be a plain number")
    if int(value.this) > max_rows:
        limit.set("expression", exp.Literal.number(max_rows))
    return root
