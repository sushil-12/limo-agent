"""Schema ownership guard, and the Mongo -> MySQL copy's parsing rules."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

from app.realtime.etl import Migrator, _as_datetime
from app.realtime.tables import RT_TABLE_PREFIX, rt_metadata
from tests.realtime.conftest import DRIVER, LIVE_RIDE, RIDER

VERSIONS = Path(__file__).resolve().parents[2] / "app/realtime/migrations/versions"
# op.<fn>(table, ...) and op.<fn>(name, table, ...) calls that name a table.
TABLE_FIRST = {"create_table", "drop_table", "add_column", "drop_column", "alter_column", "rename_table"}
TABLE_SECOND = {
    "create_index",
    "drop_index",
    "create_unique_constraint",
    "drop_constraint",
    "create_foreign_key",
}


def _tables_named(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "op"):
            continue
        index = 0 if node.func.attr in TABLE_FIRST else 1 if node.func.attr in TABLE_SECOND else None
        if node.func.attr == "execute":
            raise AssertionError(f"{path.name}: raw op.execute is not allowed in limo-rt migrations")
        if index is not None and len(node.args) > index:
            arg = node.args[index]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in {"source", "table"}:
                continue  # helper parameters, checked at their call sites
            else:
                raise AssertionError(f"{path.name}: op.{node.func.attr} with a non-literal table name")
    return names


def test_migrations_only_touch_rt_tables() -> None:
    revisions = sorted(VERSIONS.glob("*.py"))
    assert revisions
    for path in revisions:
        for name in _tables_named(path):
            # create_foreign_key's *referenced* table is Laravel's; it is only referenced.
            assert name.startswith(RT_TABLE_PREFIX) or name.startswith(("ix_", "uq_", "fk_")), (
                path.name,
                name,
            )


def test_owned_metadata_is_only_rt_tables() -> None:
    assert all(name.startswith(RT_TABLE_PREFIX) for name in rt_metadata.tables)


def test_as_datetime_accepts_mongo_shapes() -> None:
    expected = datetime(2026, 10, 31, 10, 5)
    assert _as_datetime(datetime(2026, 10, 31, 10, 5, tzinfo=UTC)) == expected
    assert _as_datetime("2026-10-31T10:05:00.000Z") == expected
    assert _as_datetime(1_793_441_100_000) == expected
    assert _as_datetime("garbage") is None


class _FakeCollection:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs

    def find(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return self.docs


def test_dry_run_counts_skips_and_downsamples(rt_db_path: Path) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    points = [  # every 2 s, ~2 m apart: 10 s rule keeps one in five
        {
            "latitude": 40.74 + i * 0.00002,
            "longitude": -73.99,
            "recordedAt": now - timedelta(seconds=100 - i * 2),
        }
        for i in range(50)
    ]
    points.append({"latitude": 40.7, "longitude": -73.9, "recordedAt": now - timedelta(days=500)})  # too old
    mongo = {
        "messages": _FakeCollection(
            [
                {
                    "_id": "a" * 24,
                    "bookingId": LIVE_RIDE,
                    "senderId": str(DRIVER),
                    "receiverId": str(RIDER),
                    "senderRole": "driver",
                    "message": "hi",
                    "createdAt": now,
                },
                {
                    "_id": "b" * 24,
                    "bookingId": 999,
                    "senderId": str(DRIVER),
                    "message": "x",
                    "createdAt": now,
                },
            ]
        ),
        "notifications": _FakeCollection(
            [
                {"_id": "c" * 24, "userId": str(RIDER), "title": "t", "message": "m", "createdAt": now},
                {"_id": "d" * 24, "topicId": "general", "title": "t", "message": "m", "createdAt": now},
            ]
        ),
        "ride_history": _FakeCollection(
            [
                {
                    "_id": "e" * 24,
                    "bookingId": str(LIVE_RIDE),
                    "driverId": str(DRIVER),
                    "ride_locations": points,
                }
            ]
        ),
    }
    migrator = Migrator(
        create_engine(f"sqlite:///{rt_db_path}"), mongo, dry_run=True, months=12, archive=None
    )
    migrator.chat()
    migrator.inbox()
    migrator.trails()
    c = migrator.report.counts
    assert c["chat.ready"] == 1
    assert c["chat.skipped.unknown_booking"] == 1
    assert c["inbox.ready"] == 1
    assert c["inbox.skipped.topic_only"] == 1
    assert c["trail.points_read"] == 51
    assert c["trail.points_older_than_retention"] == 1
    assert c["trail.points_after_downsample"] == 10
