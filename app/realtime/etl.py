"""One-time copy of limoRTService's MongoDB data into limo-rt's MySQL tables.

    .venv/bin/python -m app.realtime.etl --mongo-uri mongodb://... --mongo-db limo_rt [--dry-run]

* chat (``messages``)            -> tbl_rt_chat_messages
* inbox (``notifications``)      -> tbl_rt_notifications (personal ones; topic-only skipped)
* trails (``ride_history``)      -> tbl_rt_location_points, downsampled to one point per
  10 s or 25 m. Points older than --months (12) are not loaded; with --archive-dir they
  are written there as gzipped JSONL instead.

Idempotent: every row carries ``legacy_mongo_id`` and is written with INSERT IGNORE, so
the script can run again (the final delta at cutover) without duplicating anything.
Rows pointing at bookings or users that no longer exist are counted and skipped, never
forced in. Connects as the runtime writer (RT_DB_USERNAME) from .env.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
from sqlalchemy import URL, Engine, create_engine, select
from sqlalchemy.dialects import mysql

from app.realtime.db import utcnow
from app.realtime.geo import haversine_m
from app.realtime.settings import RealtimeSettings
from app.realtime.tables import accounts, chat_messages, location_points, notifications, reservations, users

BATCH = 1000
ROLE_MAP = {"customer": "rider", "driver": "driver", "admin": "admin"}


@dataclass
class Report:
    counts: Counter[str] = field(default_factory=Counter)

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] += n

    def print(self) -> None:
        for key in sorted(self.counts):
            print(f"  {key:<40} {self.counts[key]:>10}")


def _as_datetime(value: Any) -> datetime | None:
    """Mongo Date, ISO string or epoch (s/ms) -> naive UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC).replace(tzinfo=None)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return _as_datetime(float(value)) if value.replace(".", "", 1).isdigit() else None
        return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed
    return None


def _int(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _batches(rows: Iterator[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


class Migrator:
    def __init__(
        self, engine: Engine, mongo_db: Any, *, dry_run: bool, months: int, archive: Path | None
    ) -> None:
        self.engine = engine
        self.mongo = mongo_db
        self.dry_run = dry_run
        self.cutoff = utcnow() - timedelta(days=30 * months)
        self.archive = archive
        self.report = Report()
        with engine.connect() as conn:
            self.ride_ids = set(conn.execute(select(reservations.c.id)).scalars())
            self.user_ids = set(conn.execute(select(users.c.id)).scalars())
            self.account_user = dict(conn.execute(select(accounts.c.id, accounts.c.user_id)).all())

    def _write(self, table: Any, rows: list[dict[str, Any]], key: str) -> None:
        if not rows:
            return
        if not self.dry_run:
            with self.engine.begin() as conn:
                result = conn.execute(mysql.insert(table).prefix_with("IGNORE"), rows)
                self.report.add(f"{key}.inserted", max(result.rowcount or 0, 0))
        self.report.add(f"{key}.ready", len(rows))

    # --- chat --------------------------------------------------------------------------

    def chat(self) -> None:
        def rows() -> Iterator[dict[str, Any]]:
            for doc in self.mongo["messages"].find({}, batch_size=BATCH):
                self.report.add("chat.read")
                ride, sender = _int(doc.get("bookingId")), _int(doc.get("senderId"))
                receiver = _int(doc.get("receiverId"))
                created = _as_datetime(doc.get("createdAt"))
                body = str(doc.get("message") or "").strip()
                if ride not in self.ride_ids:
                    self.report.add("chat.skipped.unknown_booking")
                    continue
                if sender not in self.user_ids:
                    self.report.add("chat.skipped.unknown_sender")
                    continue
                if not body or created is None:
                    self.report.add("chat.skipped.empty_or_undated")
                    continue
                yield {
                    "reservation_id": ride,
                    "sender_user_id": sender,
                    "receiver_user_id": receiver if receiver in self.user_ids else None,
                    "sender_role": ROLE_MAP.get(str(doc.get("senderRole")), "rider"),
                    "body": body[:2000],
                    "client_msg_id": None,
                    "read_at": _as_datetime(doc.get("readAt")),
                    "created_at": created,
                    "legacy_mongo_id": str(doc["_id"]),
                }

        for batch in _batches(rows()):
            self._write(chat_messages, batch, "chat")

    # --- notification inbox ------------------------------------------------------------

    def inbox(self) -> None:
        def rows() -> Iterator[dict[str, Any]]:
            for doc in self.mongo["notifications"].find({}, batch_size=BATCH):
                self.report.add("inbox.read")
                user = _int(doc.get("originalUserId") or doc.get("userId"))
                if user is None:
                    self.report.add("inbox.skipped.topic_only")
                    continue
                if user not in self.user_ids:
                    self.report.add("inbox.skipped.unknown_user")
                    continue
                created = _as_datetime(doc.get("createdAt")) or utcnow()
                if created < self.cutoff:
                    self.report.add("inbox.skipped.older_than_retention")
                    continue
                data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
                ride = _int(data.get("bookingId") or data.get("booking_id"))
                yield {
                    "user_id": user,
                    "title": str(doc.get("title") or "")[:255] or "Notification",
                    "body": str(doc.get("message") or ""),
                    "type": str(doc.get("type") or "info")[:50],
                    "priority": str(doc.get("priority") or "normal")[:16],
                    "data": orjson.loads(orjson.dumps(data, default=str)) or None,
                    "reservation_id": ride if ride in self.ride_ids else None,
                    "read_at": _as_datetime(doc.get("readAt")),
                    "created_at": created,
                    "legacy_mongo_id": str(doc["_id"]),
                }

        for batch in _batches(rows()):
            self._write(notifications, batch, "inbox")

    # --- ride trails -------------------------------------------------------------------

    def trails(self) -> None:
        with contextlib.ExitStack() as stack:
            archive_fh = None
            if self.archive is not None:
                self.archive.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                path = self.archive / f"ride_history_older_{stamp}.jsonl.gz"
                archive_fh = stack.enter_context(gzip.open(path, "wt", encoding="utf-8"))
            for doc in self.mongo["ride_history"].find({}, batch_size=50):
                self.report.add("trail.rides_read")
                ride = _int(doc.get("bookingId"))
                if ride not in self.ride_ids:
                    self.report.add("trail.rides_skipped.unknown_booking")
                    continue
                driver = _int(doc.get("driverId"))
                kept, old = self._downsample(doc.get("ride_locations") or [])
                if old and archive_fh is not None:
                    archive_fh.write(
                        orjson.dumps({"bookingId": ride, "points": old}, default=str).decode() + "\n"
                    )
                    self.report.add("trail.points_archived", len(old))
                elif old:
                    self.report.add("trail.points_older_than_retention", len(old))
                rows = [
                    {
                        "reservation_id": ride,
                        "driver_user_id": driver if driver in self.user_ids else None,
                        "recorded_at": p["at"],
                        "lat": round(p["lat"], 7),
                        "lng": round(p["lng"], 7),
                        "accuracy_m": p.get("acc"),
                        "speed_mps": p.get("speed"),
                        "heading": p.get("heading"),
                        "source": "legacy",
                        "legacy_mongo_id": str(doc["_id"]),
                        "created_at": utcnow(),
                    }
                    for p in kept
                ]
                for batch in _batches(iter(rows)):
                    self._write(location_points, batch, "trail.points")

    def _downsample(self, raw: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        points: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            at = _as_datetime(item.get("recordedAt") or item.get("timestamp"))
            try:
                lat, lng = float(item["latitude"]), float(item["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            if at is None or lat == 0 or lng == 0:
                continue
            points.append(
                {
                    "at": at,
                    "lat": lat,
                    "lng": lng,
                    "acc": _num(item.get("accuracy"), 99999.99),
                    "speed": _num(item.get("speed"), 9999.99),
                    "heading": _num(item.get("heading"), 9999.9),
                }
            )
        points.sort(key=lambda p: p["at"])
        self.report.add("trail.points_read", len(points))
        kept: list[dict[str, Any]] = []
        old: list[dict[str, Any]] = []
        last: dict[str, Any] | None = None
        for p in points:
            if p["at"] < self.cutoff:
                old.append(p)
                continue
            if (
                last is None
                or (p["at"] - last["at"]).total_seconds() >= 10
                or haversine_m(last["lat"], last["lng"], p["lat"], p["lng"]) >= 25
            ):
                kept.append(p)
                last = p
        self.report.add("trail.points_after_downsample", len(kept))
        return kept, old


def _num(value: Any, limit: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if -1 <= number <= limit else None


def _engine(settings: RealtimeSettings) -> Engine:
    return create_engine(
        URL.create(
            "mysql+pymysql",
            username=settings.rt_db_username,
            password=settings.rt_db_password.get_secret_value(),
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_database,
            query={"charset": "utf8mb4"},
        ),
        connect_args={"init_command": f"SET time_zone = '{settings.db_timezone}'"},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mongo-uri", required=True)
    parser.add_argument("--mongo-db", required=True)
    parser.add_argument("--only", choices=["chat", "inbox", "trails"], action="append")
    parser.add_argument("--months", type=int, default=12, help="trail and inbox retention to load")
    parser.add_argument("--archive-dir", type=Path, help="write trail points older than --months here")
    parser.add_argument("--dry-run", action="store_true", help="read and count, write nothing")
    args = parser.parse_args(argv)

    from pymongo import MongoClient  # only this script needs pymongo

    settings = RealtimeSettings()  # type: ignore[call-arg]
    engine = _engine(settings)
    mongo = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=10_000)[args.mongo_db]
    migrator = Migrator(engine, mongo, dry_run=args.dry_run, months=args.months, archive=args.archive_dir)
    steps = args.only or ["chat", "inbox", "trails"]
    for step in steps:
        print(f"== {step}{' (dry run)' if args.dry_run else ''}")
        {"chat": migrator.chat, "inbox": migrator.inbox, "trails": migrator.trails}[step]()
    print("== report")
    migrator.report.print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
