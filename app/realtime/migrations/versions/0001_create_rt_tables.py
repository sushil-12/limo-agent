"""Create limo-rt's own tables: location points, chat, devices, notifications, settings.

Revision ID: 0001_rt_tables
Revises:
Create Date: 2026-10-31

Foreign keys point at Laravel tables, but are created from limo-rt's side only: Laravel's
tables are read (information_schema), never altered. The referencing column copies the
exact type of the referenced key (``bigint(20) unsigned`` vs ``bigint(20)`` ...), because
MariaDB refuses a foreign key whose types differ even in signedness. If a referenced table
is not InnoDB, the key is skipped with a warning rather than failing the deploy.

ON DELETE: deleting a booking or user removes its chat and devices (CASCADE); a deleted
receiver becomes NULL. Laravel mostly soft-deletes, but a hard delete must never fail
because of these tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0001_rt_tables"
down_revision = None
branch_labels = None
depends_on = None

TABLE_ARGS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


def _referenced_type(table: str, column: str) -> sa.types.TypeEngine:
    """The exact integer type of ``table.column`` in the live database."""
    column_type = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
            ),
            {"t": table, "c": column},
        )
        .scalar()
    )
    if column_type is None:
        raise RuntimeError(f"{table}.{column} not found: run against the Laravel database")
    unsigned = "unsigned" in column_type.lower()
    if column_type.lower().startswith("bigint"):
        return mysql.BIGINT(unsigned=unsigned)
    if column_type.lower().startswith("int"):
        return mysql.INTEGER(unsigned=unsigned)
    raise RuntimeError(f"unexpected key type {column_type!r} for {table}.{column}")


def _is_innodb(table: str) -> bool:
    engine = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT ENGINE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
            ),
            {"t": table},
        )
        .scalar()
    )
    return str(engine or "").lower() == "innodb"


def _fk(name: str, source: str, column: str, target: str, ondelete: str) -> None:
    if not _is_innodb(target):
        print(f"WARNING: {target} is not InnoDB; skipping foreign key {name}")
        return
    op.create_foreign_key(name, source, target, [column], ["id"], ondelete=ondelete)


def upgrade() -> None:
    reservation_id = _referenced_type("tbl_reservations", "id")
    user_id = _referenced_type("users", "id")

    op.create_table(
        "tbl_rt_location_points",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("reservation_id", reservation_id, nullable=False),
        sa.Column("driver_user_id", user_id, nullable=True),
        sa.Column("recorded_at", mysql.DATETIME(fsp=3), nullable=False),
        sa.Column("lat", sa.Numeric(10, 7), nullable=False),
        sa.Column("lng", sa.Numeric(10, 7), nullable=False),
        sa.Column("accuracy_m", sa.Numeric(7, 2), nullable=True),
        sa.Column("speed_mps", sa.Numeric(6, 2), nullable=True),
        sa.Column("heading", sa.Numeric(5, 1), nullable=True),
        sa.Column("source", sa.String(16), nullable=True),
        sa.Column("legacy_mongo_id", sa.String(24), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False),
        **TABLE_ARGS,
    )
    # No foreign key here on purpose: this is the high-volume insert table.
    op.create_index("ix_rt_points_ride_time", "tbl_rt_location_points", ["reservation_id", "recorded_at"])
    op.create_index("ix_rt_points_time", "tbl_rt_location_points", ["recorded_at"])
    op.create_index(
        "uq_rt_points_legacy", "tbl_rt_location_points", ["legacy_mongo_id", "recorded_at"], unique=True
    )

    op.create_table(
        "tbl_rt_chat_messages",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("reservation_id", reservation_id, nullable=False),
        sa.Column("sender_user_id", user_id, nullable=False),
        sa.Column("receiver_user_id", user_id, nullable=True),
        sa.Column("sender_role", sa.String(16), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("client_msg_id", sa.String(64), nullable=True),
        sa.Column("read_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False),
        sa.Column("legacy_mongo_id", sa.String(24), nullable=True),
        sa.UniqueConstraint("sender_user_id", "client_msg_id", name="uq_rt_chat_client_msg"),
        sa.UniqueConstraint("legacy_mongo_id", name="uq_rt_chat_legacy"),
        **TABLE_ARGS,
    )
    op.create_index("ix_rt_chat_ride", "tbl_rt_chat_messages", ["reservation_id", "id"])
    _fk("fk_rt_chat_ride", "tbl_rt_chat_messages", "reservation_id", "tbl_reservations", "CASCADE")
    _fk("fk_rt_chat_sender", "tbl_rt_chat_messages", "sender_user_id", "users", "CASCADE")
    _fk("fk_rt_chat_receiver", "tbl_rt_chat_messages", "receiver_user_id", "users", "SET NULL")

    op.create_table(
        "tbl_rt_user_devices",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("user_id", user_id, nullable=False),
        sa.Column("app", sa.String(16), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("fcm_token", sa.String(255), nullable=False),
        sa.Column("app_version", sa.String(32), nullable=True),
        sa.Column("locale", sa.String(16), nullable=True),
        sa.Column("last_seen_at", mysql.DATETIME(fsp=3), nullable=False),
        sa.Column("revoked_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False),
        sa.UniqueConstraint("fcm_token", name="uq_rt_devices_token"),
        **TABLE_ARGS,
    )
    op.create_index("ix_rt_devices_user", "tbl_rt_user_devices", ["user_id", "revoked_at"])
    _fk("fk_rt_devices_user", "tbl_rt_user_devices", "user_id", "users", "CASCADE")

    op.create_table(
        "tbl_rt_notifications",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("user_id", user_id, nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("priority", sa.String(16), nullable=False),
        sa.Column("data", sa.JSON, nullable=True),
        sa.Column("reservation_id", reservation_id, nullable=True),
        sa.Column("read_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False),
        sa.Column("legacy_mongo_id", sa.String(24), nullable=True),
        sa.UniqueConstraint("legacy_mongo_id", name="uq_rt_notifications_legacy"),
        **TABLE_ARGS,
    )
    op.create_index("ix_rt_notifications_user", "tbl_rt_notifications", ["user_id", "id"])

    op.create_table(
        "tbl_rt_settings",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
        sa.Column("updated_by_user_id", user_id, nullable=True),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), nullable=False),
        **TABLE_ARGS,
    )


def downgrade() -> None:
    for table in (
        "tbl_rt_settings",
        "tbl_rt_notifications",
        "tbl_rt_user_devices",
        "tbl_rt_chat_messages",
        "tbl_rt_location_points",
    ):
        op.drop_table(table)
