"""Worked question -> SQL examples shown to the model (few-shot "training").

Examples live in ``sql_examples.jsonl`` next to this file, one JSON object per line:

    {"audience": "admin", "question": "how many pending bookings", "sql": "SELECT ..."}

``audience`` is "admin" (platform-wide queries) or "scoped" (riders/drivers/travel agents,
whose tables already contain only their own rows, so the SQL never filters by user). For
each question the most similar examples of the same audience go into the prompt, skipping
examples that use a table the requester can't query.
To fix a recurring mistake, find the question in the query log, write the correct
SQL, and add the pair here.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import orjson
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.core.logging import get_logger
from app.services.data.catalog import words
from app.services.data.sql_cache import normalise

logger = get_logger(__name__)

EXAMPLES_PATH = Path(__file__).with_name("sql_examples.jsonl")
Audience = Literal["admin", "scoped"]
_STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "by",
        "do",
        "does",
        "for",
        "from",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "many",
        "me",
        "much",
        "my",
        "of",
        "on",
        "or",
        "our",
        "show",
        "the",
        "there",
        "this",
        "to",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "which",
        "who",
        "with",
        "you",
        "your",
    ]
)


@dataclass(frozen=True, slots=True)
class Example:
    audience: Audience
    question: str
    sql: str
    words: frozenset[str]
    tables: frozenset[str]


def load_examples(path: Path = EXAMPLES_PATH) -> list[Example]:
    """Read the examples file; bad lines are skipped with a warning."""
    if not path.exists():
        return []
    examples: list[Example] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            item = orjson.loads(line)
            audience, question, sql = item["audience"], item["question"], item["sql"]
            if audience not in ("admin", "scoped"):
                raise ValueError("audience must be 'admin' or 'scoped'")
        except (orjson.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("sql_example_skipped", line=number, error=str(exc))
            continue
        examples.append(_example(audience, question, sql))
    return examples


def save_example(
    audience: Audience, question: str, sql: str, path: Path = EXAMPLES_PATH
) -> tuple[Example, bool]:
    """Write a pair to the examples file, replacing the one for the same question and audience.

    Returns the example and whether an existing pair was replaced. Comments and other
    lines are kept as they are.
    """
    example = _example(audience, question.strip(), sql)
    line = json.dumps(
        {"audience": audience, "question": example.question, "sql": example.sql}, ensure_ascii=False
    )
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    kept: list[str] = []
    replaced = False
    for existing in lines:
        if _same_question(existing, audience, example.question):
            if not replaced:
                kept.append(line)
            replaced = True
        else:
            kept.append(existing)
    if not replaced:
        kept.append(line)
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return example, replaced


def _example(audience: Audience, question: str, sql: str) -> Example:
    return Example(audience, question, " ".join(sql.split()), frozenset(words(question)), _tables(sql))


def _tables(sql: str) -> frozenset[str]:
    """Tables the query reads (CTE names excluded); empty if it doesn't parse."""
    try:
        tree = sqlglot.parse_one(sql, read="mysql")
    except SqlglotError:
        return frozenset()
    ctes = {cte.alias for cte in tree.find_all(exp.CTE)}
    return frozenset(t.name for t in tree.find_all(exp.Table) if t.name not in ctes)


def _same_question(line: str, audience: Audience, question: str) -> bool:
    if not line.strip() or line.lstrip().startswith("//"):
        return False
    try:
        item = orjson.loads(line)
        return item["audience"] == audience and normalise(item["question"]) == normalise(question)
    except (orjson.JSONDecodeError, KeyError, TypeError):
        return False


def pick_examples(
    examples: list[Example],
    question: str,
    audience: Audience,
    *,
    limit: int = 3,
    tables: Collection[str] | None = None,
) -> list[Example]:
    """The ``limit`` examples sharing most words with ``question`` (at least one).

    With ``tables``, only examples reading nothing but those tables qualify.
    """
    asked = words(question) - _STOP_WORDS
    scored = [
        (len(asked & e.words), e)
        for e in examples
        if e.audience == audience and (tables is None or e.tables <= set(tables))
    ]
    scored = [(s, e) for s, e in scored if s > 0]
    scored.sort(key=lambda pair: -pair[0])
    return [e for _, e in scored[:limit]]


def render_examples(examples: list[Example]) -> str:
    if not examples:
        return ""
    lines = ["Examples of correct queries:"]
    for example in examples:
        lines += [f"Q: {example.question}", f"SQL: {example.sql}"]
    return "\n".join(lines) + "\n"
